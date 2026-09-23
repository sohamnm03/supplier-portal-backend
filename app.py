import hashlib
import hmac
import json
import secrets
from datetime import date, datetime
from decimal import Decimal

import requests
from flask import Flask, jsonify, request
from flask_cors import CORS
from mysql.connector import Error as MySQLError

from db import get_connection

app = Flask(__name__)
CORS(app, origins=["http://localhost:5173", "http://127.0.0.1:5173"])

EMAIL_API_URL = "http://127.0.0.1:8000/api/email/send"

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
    try:
        conn = get_connection()
        cursor = conn.cursor(dictionary=True)
        columns = ", ".join(VENDOR_RESPONSE_FIELDS)
        cursor.execute(f"SELECT {columns} FROM vendor")
        rows = cursor.fetchall()
        cursor.close()
        conn.close()
    except MySQLError as err:
        return jsonify({"error": str(err)}), 500

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

@app.post("/vendors/approval")
def approve_vendor():
    data = request.get_json(silent=True) or {}

    vendor_id = data.get("vendor_id")
    isapproved = data.get("isapproved")

    if vendor_id is None:
        return jsonify({"error": "vendor_id is required"}), 400

    conn = None
    cursor = None
    try:
        conn = get_connection()
        cursor = conn.cursor(dictionary=True)

        cursor.execute(
            "SELECT vendor_id, email, vendor_legal_name, status FROM vendor "
            "WHERE vendor_id = %s",
            (vendor_id,),
        )
        vendor = cursor.fetchone()

        if not vendor:
            return jsonify({"error": "Vendor not found"}), 404

        if vendor["status"] == "active":
            return jsonify({"error": "Vendor already active"}), 409

        if isapproved is True:
            password, password_hash = generate_unique_password(cursor)
            cursor.execute(
                "UPDATE vendor SET status = %s, password = %s WHERE vendor_id = %s",
                ("active", password_hash, vendor_id),
            )
            conn.commit()
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
        else:
            message = "Vendor remains pending"

        response = {
            "message": message,
            "vendor_id": vendor_id,
            "email": vendor["email"],
            "isapproved": isapproved,
        }
        if isapproved is True:
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
