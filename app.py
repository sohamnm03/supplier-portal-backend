import hashlib
import hmac
import json
import re
import secrets
import uuid
from datetime import date, datetime, timezone
from decimal import Decimal, InvalidOperation

import requests
from flask import Flask, jsonify, request
from flask_cors import CORS
from mysql.connector import Error as MySQLError

from db import get_connection

app = Flask(__name__)
CORS(app, origins=["http://localhost:5173", "http://127.0.0.1:5173"])

EMAIL_API_URL = "http://127.0.0.1:8000/api/email/send"
INVOICE_OCR_API_URL = "http://127.0.0.1:8000/api/invoice/ocr"

VENDOR_FIELDS = [
    "title",
    "name",
    "email",
    "contact_no",
    "vendor_legal_name",
    "vendor_type",
    "vendor_category",
    "vendor_subcategory",
    "year_established",
    "currency",
    "registration_number",
    "msme_status",
    "udyam_number",
    "gstin",
    "pan",
    "aadhaar_no",
    "cin",
    "street",
    "city",
    "district",
    "region",
    "postal_code",
    "account_holder_name",
    "bank_name",
    "branch_name",
    "bank_account_no",
    "ifsc_code",
    "status",
]

VENDOR_RESPONSE_FIELDS = [
    "vendor_id",
    *VENDOR_FIELDS,
    "created_at",
    "updated_at",
    "sap_vendor",
]

INVOICE_JSON_FIELDS = {
    "vendor_gstin_data",
    "customer_gstin_data",
    "matched_invoice_ids",
    "duplicate_reasons",
    "score_breakdown",
    "duplicate_check_data",
    "raw_response",
}

LINE_ITEM_JSON_FIELDS = {"raw_item_data"}

REQUIRED_FIELDS = [
    "vendor_legal_name",
    "vendor_type",
    "vendor_category",
    "vendor_subcategory",
    "registration_number",
    "pan",
    "street",
    "city",
    "district",
    "region",
    "postal_code",
    "account_holder_name",
    "bank_name",
    "branch_name",
    "bank_account_no",
    "ifsc_code",
]


def _serialize_row(row, json_fields=()):
    """Convert DB-native types (Decimal, date/datetime, JSON text) to JSON-safe values."""
    serialized = {}
    for key, value in row.items():
        if key in json_fields and isinstance(value, (str, bytes, bytearray)):
            try:
                serialized[key] = json.loads(value)
            except (TypeError, ValueError):
                serialized[key] = value
        elif isinstance(value, Decimal):
            serialized[key] = float(value)
        elif isinstance(value, (datetime, date)):
            serialized[key] = value.isoformat()
        else:
            serialized[key] = value
    return serialized


def _decimal_value(value):
    """Convert OCR currency, percentage, and number strings to Decimal."""
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (int, float, Decimal)):
        return Decimal(str(value))

    text = str(value).strip()
    if not text:
        return None

    negative = text.startswith("(") and text.endswith(")")
    normalized = re.sub(r"[^0-9.\-]", "", text.replace(",", ""))
    if not normalized or normalized in {"-", ".", "-."}:
        return None
    try:
        result = Decimal(normalized)
        return -result if negative and result > 0 else result
    except InvalidOperation:
        return None


def _date_value(value):
    if not value:
        return None
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    try:
        return date.fromisoformat(str(value).strip()[:10])
    except ValueError:
        return None


def _datetime_value(value):
    if not value:
        return None
    if isinstance(value, datetime):
        parsed = value
    else:
        try:
            parsed = datetime.fromisoformat(str(value).strip().replace("Z", "+00:00"))
        except ValueError:
            return None
    if parsed.tzinfo is not None:
        parsed = parsed.astimezone(timezone.utc).replace(tzinfo=None)
    return parsed


def _json_value(value):
    return json.dumps(value, default=str, ensure_ascii=False)


def _normalized_invoice_number(value):
    if not value:
        return None
    return re.sub(r"[^A-Z0-9]", "", str(value).upper()) or None


def generate_unique_password(cursor):
    """Return a six-digit password and its unused SHA-256 hash."""
    for _ in range(100):
        password = f"{secrets.randbelow(1_000_000):06d}"
        password_hash = hashlib.sha256(password.encode("utf-8")).hexdigest()

        cursor.execute(
            "SELECT 1 FROM vendor WHERE password = %s LIMIT 1",
            (password_hash,),
        )
        if cursor.fetchone() is None:
            return password, password_hash

    raise RuntimeError("Unable to generate a unique vendor password")


@app.get("/vendors")
def get_vendors():
    vendor_id = request.args.get("vendor") or request.args.get("vendor_id")

    conn = None
    cursor = None
    try:
        conn = get_connection()
        cursor = conn.cursor(dictionary=True)
        columns = ", ".join(VENDOR_RESPONSE_FIELDS)

        if vendor_id:
            cursor.execute(
                f"SELECT {columns} FROM vendor WHERE vendor_id = %s LIMIT 1",
                (vendor_id,),
            )
            row = cursor.fetchone()

            if not row:
                return jsonify({"error": "Vendor not found"}), 404

            return jsonify(_serialize_row(row)), 200

        cursor.execute(f"SELECT {columns} FROM vendor")
        rows = [_serialize_row(row) for row in cursor.fetchall()]
    except MySQLError as err:
        return jsonify({"error": str(err)}), 500
    finally:
        if cursor is not None:
            cursor.close()
        if conn is not None:
            conn.close()

    return jsonify(rows), 200


@app.post("/vendors")
def create_vendor():
    data = request.get_json(silent=True)
    if not data:
        return jsonify({"error": "Request body must be JSON"}), 400

    missing = [field for field in REQUIRED_FIELDS if not data.get(field)]
    if missing:
        return jsonify({"error": f"Missing required field(s): {', '.join(missing)}"}), 400

    # Only include fields the client actually sent, so omitted columns
    # (e.g. currency, msme_status, status) fall back to their DB defaults
    # instead of being explicitly inserted as NULL.
    values = {field: data[field] for field in VENDOR_FIELDS if data.get(field) is not None}

    columns = ", ".join(values.keys())
    placeholders = ", ".join(["%s"] * len(values))
    sql = f"INSERT INTO vendor ({columns}) VALUES ({placeholders})"

    try:
        conn = get_connection()
        cursor = conn.cursor(dictionary=True)
        cursor.execute(sql, list(values.values()))
        conn.commit()
        new_vendor_id = cursor.lastrowid

        cursor.execute("SELECT * FROM vendor WHERE vendor_id = %s", (new_vendor_id,))
        row = cursor.fetchone()
        cursor.close()
        conn.close()
    except MySQLError as err:
        return jsonify({"error": str(err)}), 500

    return jsonify(row), 201

@app.patch("/vendors")
def update_vendor():
    data = request.get_json(silent=True)
    if not data:
        return jsonify({"error": "Request body must be JSON"}), 400

    vendor_id = data.get("vendor_id")
    if vendor_id is None:
        return jsonify({"error": "vendor_id is required"}), 400

    values = {field: data[field] for field in VENDOR_FIELDS if field in data}
    if not values:
        return jsonify({"error": "No updatable fields provided"}), 400

    conn = None
    cursor = None
    try:
        conn = get_connection()
        cursor = conn.cursor(dictionary=True)

        cursor.execute("SELECT vendor_id FROM vendor WHERE vendor_id = %s", (vendor_id,))
        if cursor.fetchone() is None:
            return jsonify({"error": "Vendor not found"}), 404

        assignments = ", ".join(f"{field} = %s" for field in values)
        cursor.execute(
            f"UPDATE vendor SET {assignments} WHERE vendor_id = %s",
            [*values.values(), vendor_id],
        )
        conn.commit()

        columns = ", ".join(VENDOR_RESPONSE_FIELDS)
        cursor.execute(f"SELECT {columns} FROM vendor WHERE vendor_id = %s", (vendor_id,))
        row = cursor.fetchone()
    except MySQLError as err:
        if conn is not None:
            conn.rollback()
        return jsonify({"error": str(err)}), 500
    finally:
        if cursor is not None:
            cursor.close()
        if conn is not None:
            conn.close()

    return jsonify(row), 200

@app.post("/vendors/approval")
def approve_vendor():
    data = request.get_json(silent=True) or {}

    vendor_id = data.get("vendor_id")
    status_code = data.get("status")

    if vendor_id is None:
        return jsonify({"error": "vendor_id is required"}), 400

    # JSON numbers cannot have leading zeroes, so support both 1/2 and
    # the string codes "01"/"02".
    if isinstance(status_code, bool):
        normalized_status_code = None
    elif isinstance(status_code, int):
        normalized_status_code = f"{status_code:02d}"
    elif isinstance(status_code, str):
        normalized_status_code = status_code.strip().zfill(2)
    else:
        normalized_status_code = None

    if normalized_status_code not in {"01", "02"}:
        return jsonify({"error": "status must be 01 or 02"}), 400

    conn = None
    cursor = None
    try:
        conn = get_connection()
        cursor = conn.cursor(dictionary=True)

        cursor.execute(
            "SELECT vendor_id, email, vendor_legal_name, status FROM vendor "
            "WHERE vendor_id = %s FOR UPDATE",
            (vendor_id,),
        )
        vendor = cursor.fetchone()

        if not vendor:
            return jsonify({"error": "Vendor not found"}), 404

        current_status = vendor["status"]
        normalized_current_status = str(current_status).strip().lower()

        if normalized_status_code == "01":
            if normalized_current_status != "pending":
                return jsonify(
                    {
                        "error": "Status 01 is only valid for a pending vendor",
                        "current_status": current_status,
                    }
                ), 409

            cursor.execute(
                "UPDATE vendor SET status = %s WHERE vendor_id = %s",
                ("sent for approval", vendor_id),
            )
            conn.commit()
            new_status = "sent for approval"
            message = "Vendor sent for approval"
        else:
            if normalized_current_status != "sent for approval":
                return jsonify(
                    {
                        "error": (
                            "Status 02 is only valid for a vendor that has been "
                            "sent for approval"
                        ),
                        "current_status": current_status,
                    }
                ), 409

            password, password_hash = generate_unique_password(cursor)
            cursor.execute(
                "UPDATE vendor SET status = %s, password = %s WHERE vendor_id = %s",
                ("active", password_hash, vendor_id),
            )
            conn.commit()
            new_status = "active"
            message = "Vendor approved successfully"

            email_sent = True
            try:
                requests.post(
                    EMAIL_API_URL,
                    json={
                        "to": vendor["email"],
                        "subject": "Your vendor account is ready",
                        "template_name": "vendor_account_onboarding",
                        "params": {
                            "vendor_name": vendor["vendor_legal_name"],
                            "username": vendor["email"],
                            "password": password,
                            "login_url": "http://localhost:5173/",
                            "support_email": "support@fourthsignal.com",
                        },
                    },
                    timeout=10,
                ).raise_for_status()
            except requests.RequestException:
                email_sent = False
        response = {
            "message": message,
            "vendor_id": vendor_id,
            "email": vendor["email"],
            "status": new_status,
        }
        if normalized_status_code == "02":
            response["password"] = password
            response["email_sent"] = email_sent

        return jsonify(response), 200

    except (MySQLError, RuntimeError) as err:
        if conn is not None:
            conn.rollback()
        return jsonify({"error": str(err)}), 500
    finally:
        if cursor is not None:
            cursor.close()
        if conn is not None:
            conn.close()

@app.post("/vendors/check-email")
def check_vendor_email():
    data = request.get_json(silent=True) or {}

    email = data.get("email")
    if not isinstance(email, str) or not email.strip():
        return jsonify({"error": "email is required"}), 400

    normalized_email = email.strip().lower()

    conn = None
    cursor = None
    try:
        conn = get_connection()
        cursor = conn.cursor(dictionary=True)

        cursor.execute(
            "SELECT vendor_id FROM vendor WHERE LOWER(TRIM(email)) = %s LIMIT 1",
            (normalized_email,),
        )
        vendor = cursor.fetchone()

        if vendor:
            return jsonify({"message": "Email exists", "code": 200,"exists": True, "data": []}), 200
        else:
            return jsonify({"message": "Email not found", "code": 200, "exists": False, "data": []}), 200

    except MySQLError as err:
        return jsonify({"error": str(err)}), 500
    finally:
        if cursor is not None:
            cursor.close()
        if conn is not None:
            conn.close()


@app.post("/vendors/login")
def login_vendor():
    data = request.get_json(silent=True)
    if not isinstance(data, dict):
        return jsonify({"error": "Request body must be JSON"}), 400

    email = data.get("email")
    password = data.get("password")
    if not isinstance(email, str) or not email.strip():
        return jsonify({"error": "email is required"}), 400
    if not isinstance(password, str) or not password:
        return jsonify({"error": "password is required"}), 400

    conn = None
    cursor = None
    try:
        conn = get_connection()
        cursor = conn.cursor(dictionary=True)
        cursor.execute(
            "SELECT vendor_id, email, password FROM vendor "
            "WHERE email = %s AND status = %s LIMIT 1",
            (email.strip(), "active"),
        )
        vendor = cursor.fetchone()

        submitted_hash = hashlib.sha256(password.encode("utf-8")).hexdigest()
        stored_hash = vendor.get("password") if vendor else None
        if not stored_hash or not hmac.compare_digest(str(stored_hash), submitted_hash):
            return jsonify({"error": "Invalid email or password"}), 401

        return jsonify(
            {
                "message": "Login successful",
                "vendor_id": vendor["vendor_id"],
                "email": vendor["email"],
            }
        ), 200
    except MySQLError as err:
        return jsonify({"error": str(err)}), 500
    finally:
        if cursor is not None:
            cursor.close()
        if conn is not None:
            conn.close()

@app.post("/vendors/check-pan")
def check_vendor_pan():
    data = request.get_json(silent=True) or {}

    pan = data.get("pan")
    if not isinstance(pan, str) or not pan.strip():
        return jsonify({"error": "pan is required"}), 400

    normalized_pan = pan.strip().upper()

    conn = None
    cursor = None
    try:
        conn = get_connection()
        cursor = conn.cursor(dictionary=True)

        cursor.execute(
            "SELECT vendor_id FROM vendor WHERE UPPER(TRIM(pan)) = %s LIMIT 1",
            (normalized_pan,),
        )
        vendor = cursor.fetchone()

        if vendor:
            return jsonify({"message": "PAN exists", "code": 200,"exists": True, "data": []}), 200
        else:
            return jsonify({"message": "PAN not found", "code": 200, "exists": False, "data": []}), 200

    except MySQLError as err:
        return jsonify({"error": str(err)}), 500
    finally:
        if cursor is not None:
            cursor.close()
        if conn is not None:
            conn.close()


@app.post("/invoices/ocr")
def upload_invoice_for_ocr():
    vendor_id = request.form.get("vendor_id", "").strip()
    invoice_file = request.files.get("invoices")

    if not vendor_id:
        return jsonify({"error": "vendor_id is required"}), 400
    if invoice_file is None or not invoice_file.filename:
        return jsonify({"error": "invoices file is required"}), 400

    # Reject an invalid vendor before uploading the file to the OCR service.
    validation_conn = None
    validation_cursor = None
    try:
        validation_conn = get_connection()
        validation_cursor = validation_conn.cursor(dictionary=True)
        validation_cursor.execute(
            "SELECT vendor_id FROM vendor WHERE vendor_id = %s LIMIT 1",
            (vendor_id,),
        )
        if validation_cursor.fetchone() is None:
            return jsonify({"error": "Vendor not found"}), 404
    except MySQLError as err:
        return jsonify({"error": str(err)}), 500
    finally:
        if validation_cursor is not None:
            validation_cursor.close()
        if validation_conn is not None:
            validation_conn.close()

    file_bytes = invoice_file.read()
    if not file_bytes:
        return jsonify({"error": "invoices file must not be empty"}), 400

    file_id = f"invoice-{uuid.uuid4().hex}"
    file_sha256 = hashlib.sha256(file_bytes).hexdigest()

    try:
        ocr_response = requests.post(
            INVOICE_OCR_API_URL,
            files={
                "invoices": (
                    invoice_file.filename,
                    file_bytes,
                    invoice_file.mimetype or "application/octet-stream",
                )
            },
            data={"file_ids": file_id},
            timeout=180,
        )
        ocr_response.raise_for_status()
        ocr_data = ocr_response.json()
    except requests.RequestException as err:
        details = None
        if getattr(err, "response", None) is not None:
            try:
                details = err.response.json()
            except ValueError:
                details = err.response.text[:1000]
        return jsonify(
            {"error": "Invoice OCR request failed", "details": details}
        ), 502
    except ValueError:
        return jsonify({"error": "Invoice OCR service returned invalid JSON"}), 502

    if not isinstance(ocr_data, dict):
        return jsonify({"error": "Invoice OCR service returned an invalid response"}), 502

    results = ocr_data.get("results") or []
    result = results[0] if results and isinstance(results[0], dict) else {}
    parsed_invoices = ocr_data.get("invoices") or []
    invoice_data = result.get("invoice") or (
        parsed_invoices[0]
        if parsed_invoices and isinstance(parsed_invoices[0], dict)
        else {}
    )
    saved_files = ocr_data.get("saved_files") or []
    file_data = result.get("file") or (
        saved_files[0]
        if saved_files and isinstance(saved_files[0], dict)
        else {}
    )
    duplicate_data = (
        result.get("duplicate_check")
        or invoice_data.get("duplicate_check")
        or {}
    )
    vendor_gstin = (
        result.get("gstin_verify") or invoice_data.get("gstin_verify") or {}
    )
    customer_gstin = (
        result.get("customer_gstin_verify")
        or invoice_data.get("customer_gstin_verify")
        or {}
    )
    vendor_gstin_details = vendor_gstin.get("data") or {}
    customer_gstin_details = customer_gstin.get("data") or {}

    invoice_number = invoice_data.get("invoice_number") or invoice_data.get("invoice_id")
    blob_path = file_data.get("blob_path")
    blob_name = file_data.get("blob_name")
    invoice_values = {
        "vendor_id": vendor_id,
        "file_id": file_id,
        "original_file_name": file_data.get("original_name") or invoice_file.filename,
        "saved_file_name": blob_name,
        "saved_file_path": blob_path,
        "file_size_bytes": file_data.get("size") or len(file_bytes),
        "mime_type": file_data.get("mime_type") or invoice_file.mimetype,
        "file_sha256": file_sha256,
        "cached_at": _datetime_value(file_data.get("cached_at")),
        "invoice_number": invoice_number,
        "normalized_invoice_number": _normalized_invoice_number(invoice_number),
        "invoice_date": _date_value(invoice_data.get("invoice_date")),
        "due_date": _date_value(invoice_data.get("due_date")),
        "service_start_date": _date_value(invoice_data.get("service_start_date")),
        "service_end_date": _date_value(invoice_data.get("service_end_date")),
        "purchase_order": invoice_data.get("purchase_order"),
        "payment_term": invoice_data.get("payment_term"),
        "currency": invoice_data.get("currency"),
        "subtotal": _decimal_value(invoice_data.get("sub_total")),
        "taxable_total": _decimal_value(invoice_data.get("taxable_total")),
        "total_tax": _decimal_value(invoice_data.get("total_tax")),
        "total_amount": _decimal_value(
            invoice_data.get("total_amount") or invoice_data.get("invoice_total")
        ),
        "vendor_name": invoice_data.get("vendor_name"),
        "extracted_vendor_name": invoice_data.get("extracted_vendor_name"),
        "vendor_tax_id": invoice_data.get("vendor_tax_id"),
        "vendor_address": invoice_data.get("vendor_address"),
        "vendor_address_recipient": invoice_data.get("vendor_address_recipient"),
        "customer_id": invoice_data.get("customer_id"),
        "customer_name": invoice_data.get("customer_name"),
        "customer_tax_id": invoice_data.get("customer_tax_id"),
        "billing_address": invoice_data.get("billing_address"),
        "billing_address_recipient": invoice_data.get("billing_address_recipient"),
        "shipping_address": invoice_data.get("shipping_address"),
        "shipping_address_recipient": invoice_data.get("shipping_address_recipient"),
        "confidence": _decimal_value(invoice_data.get("confidence")),
        "vendor_gstin_verified": vendor_gstin.get("valid"),
        "vendor_gstin_status": vendor_gstin_details.get("status"),
        "vendor_gstin_trade_name": vendor_gstin_details.get("tradeName"),
        "vendor_gstin_legal_name": vendor_gstin_details.get("legalName"),
        "vendor_gstin_data": _json_value(vendor_gstin) if vendor_gstin else None,
        "customer_gstin_verified": customer_gstin.get("valid"),
        "customer_gstin_status": customer_gstin_details.get("status"),
        "customer_gstin_trade_name": customer_gstin_details.get("tradeName"),
        "customer_gstin_legal_name": customer_gstin_details.get("legalName"),
        "customer_gstin_data": _json_value(customer_gstin) if customer_gstin else None,
        "source_channel": duplicate_data.get("source_channel"),
        "pipeline_phase": duplicate_data.get("pipeline_phase"),
        "pipeline_version": duplicate_data.get("pipeline_version"),
        "duplicate_status": duplicate_data.get("duplicate_status"),
        "duplicate_decision": duplicate_data.get("duplicate_decision"),
        "duplicate_score": _decimal_value(duplicate_data.get("duplicate_score")),
        "duplicate_risk_level": duplicate_data.get("duplicate_risk_level"),
        "recommended_action": duplicate_data.get("recommended_action"),
        "duplicate_evaluated_at": _datetime_value(duplicate_data.get("evaluated_at")),
        "block_id": duplicate_data.get("block_id"),
        "matched_invoice_ids": _json_value(duplicate_data.get("matched_invoice_ids", [])),
        "duplicate_reasons": _json_value(duplicate_data.get("duplicate_reasons", [])),
        "score_breakdown": _json_value(duplicate_data.get("score_breakdown", [])),
        "duplicate_check_data": _json_value(duplicate_data) if duplicate_data else None,
        "raw_response": _json_value(ocr_data),
        "status": "pending",
        "blob_url": blob_path,
        "blob_name": blob_name,
    }

    conn = None
    cursor = None
    try:
        conn = get_connection()
        cursor = conn.cursor()

        columns = ", ".join(invoice_values)
        placeholders = ", ".join(["%s"] * len(invoice_values))
        cursor.execute(
            f"INSERT INTO invoices ({columns}) VALUES ({placeholders})",
            list(invoice_values.values()),
        )
        database_invoice_id = cursor.lastrowid

        for tax in invoice_data.get("tax_details") or []:
            cursor.execute(
                "INSERT INTO invoice_tax_details "
                "(invoice_id, tax_type, tax_description, tax_rate, tax_amount) "
                "VALUES (%s, %s, %s, %s, %s)",
                (
                    database_invoice_id,
                    tax.get("tax_type") or tax.get("tax_desc"),
                    tax.get("tax_description") or tax.get("tax_desc"),
                    _decimal_value(tax.get("rate")),
                    _decimal_value(tax.get("amount")),
                ),
            )

        normalized_items = invoice_data.get("line_items") or []
        source_items = invoice_data.get("items") or []
        line_items = normalized_items or source_items
        for index, line_item in enumerate(line_items, start=1):
            source_item = source_items[index - 1] if index <= len(source_items) else {}
            combined_item = {**source_item, **line_item}
            raw_tax = combined_item.get("tax")
            tax_amount = (
                _decimal_value(raw_tax.get("amount"))
                if isinstance(raw_tax, dict)
                else _decimal_value(raw_tax)
            )
            extracted_tax_text = (
                _json_value(raw_tax) if isinstance(raw_tax, (dict, list)) else raw_tax
            )
            cursor.execute(
                "INSERT INTO invoice_line_items "
                "(invoice_id, line_number, product_code, description, item_date, "
                "quantity, unit, unit_price, taxable_amount, tax_amount, "
                "total_amount, extracted_tax_text, raw_item_data) "
                "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)",
                (
                    database_invoice_id,
                    index,
                    combined_item.get("product_code"),
                    combined_item.get("description"),
                    _date_value(combined_item.get("date")),
                    _decimal_value(combined_item.get("quantity")),
                    combined_item.get("unit"),
                    _decimal_value(combined_item.get("unit_price")),
                    _decimal_value(
                        combined_item.get("taxable_amount")
                        or combined_item.get("amount")
                    ),
                    tax_amount,
                    _decimal_value(
                        combined_item.get("total_amount")
                        or combined_item.get("amount")
                    ),
                    extracted_tax_text,
                    _json_value(combined_item),
                ),
            )

        conn.commit()
    except (MySQLError, TypeError, ValueError) as err:
        if conn is not None:
            conn.rollback()
        return jsonify({"error": f"Unable to save OCR response: {err}"}), 500
    finally:
        if cursor is not None:
            cursor.close()
        if conn is not None:
            conn.close()

    return jsonify(
        {
            "message": "Invoice processed and saved successfully",
            "invoice_id": database_invoice_id,
            "file_id": file_id,
            "ocr_response": ocr_data,
        }
    ), 201


@app.get("/invoices")
def get_invoices():
    vendor_id = request.args.get("vendor_id")
    if not vendor_id:
        return jsonify({"error": "vendor_id is required"}), 400

    conn = None
    cursor = None
    try:
        conn = get_connection()
        cursor = conn.cursor(dictionary=True)

        cursor.execute(
            "SELECT * FROM invoices WHERE vendor_id = %s ORDER BY id",
            (vendor_id,),
        )
        invoices = cursor.fetchall()

        if not invoices:
            return jsonify([]), 200

        invoice_ids = [invoice["id"] for invoice in invoices]
        placeholders = ", ".join(["%s"] * len(invoice_ids))

        cursor.execute(
            f"SELECT * FROM invoice_tax_details WHERE invoice_id IN ({placeholders}) "
            "ORDER BY id",
            invoice_ids,
        )
        tax_details_by_invoice = {}
        for row in cursor.fetchall():
            tax_details_by_invoice.setdefault(row["invoice_id"], []).append(
                _serialize_row(row, INVOICE_JSON_FIELDS)
            )

        cursor.execute(
            f"SELECT * FROM invoice_line_items WHERE invoice_id IN ({placeholders}) "
            "ORDER BY invoice_id, line_number",
            invoice_ids,
        )
        line_items_by_invoice = {}
        for row in cursor.fetchall():
            line_items_by_invoice.setdefault(row["invoice_id"], []).append(
                _serialize_row(row, LINE_ITEM_JSON_FIELDS)
            )

        result = []
        for invoice in invoices:
            serialized_invoice = _serialize_row(invoice, INVOICE_JSON_FIELDS)
            serialized_invoice["tax_details"] = tax_details_by_invoice.get(invoice["id"], [])
            serialized_invoice["line_items"] = line_items_by_invoice.get(invoice["id"], [])
            result.append(serialized_invoice)
    except MySQLError as err:
        return jsonify({"error": str(err)}), 500
    finally:
        if cursor is not None:
            cursor.close()
        if conn is not None:
            conn.close()

    return jsonify(result), 200


if __name__ == "__main__":
    app.run(debug=True)
