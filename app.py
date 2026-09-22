import hashlib
import os
import secrets

import requests
from flask import Flask, jsonify, request
from flask_cors import CORS
from mysql.connector import Error as MySQLError

from db import get_connection

app = Flask(__name__)
CORS(app, origins=["http://localhost:5173", "http://127.0.0.1:5173"])

SAP_BUSINESS_PARTNER_URL = (
    "https://vhnlqds4ap01.sap.niififl.in:44300/sap/opu/odata/SAP/"
    "API_BUSINESS_PARTNER/A_BusinessPartner"
)

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


class SAPIntegrationError(RuntimeError):
    """Raised when a business partner cannot be created in SAP."""


def create_sap_business_partner(vendor_name):
    username = os.environ.get("SAP_USERNAME")
    password = os.environ.get("SAP_PASSWORD")
    if not username or not password:
        raise SAPIntegrationError("SAP credentials are not configured")

    try:
        timeout = int(os.environ.get("SAP_TIMEOUT_SECONDS", "30"))
    except ValueError as err:
        raise SAPIntegrationError("SAP_TIMEOUT_SECONDS must be an integer") from err

    verify_ssl = os.environ.get("SAP_VERIFY_SSL", "true").lower() not in {
        "0",
        "false",
        "no",
    }
    payload = {
        "BusinessPartnerCategory": "2",
        "BusinessPartnerGrouping": "9800",
        "BusinessPartnerName": vendor_name,
        "OrganizationBPName1": vendor_name,
        "SearchTerm1": "ORICA",
        "SearchTerm2": "FINANCE",
        "CorrespondenceLanguage": "EN",
    }

    with requests.Session() as session:
        session.auth = (username, password)
        try:
            token_response = session.get(
                SAP_BUSINESS_PARTNER_URL,
                headers={
                    "Accept": "application/json",
                    "X-CSRF-Token": "Fetch",
                },
                timeout=timeout,
                verify=verify_ssl,
            )
            token_response.raise_for_status()

            csrf_token = token_response.headers.get("X-CSRF-Token")
            if not csrf_token:
                raise SAPIntegrationError("SAP did not return an X-CSRF-Token")

            create_response = session.post(
                SAP_BUSINESS_PARTNER_URL,
                headers={
                    "Accept": "application/json",
                    "Content-Type": "application/json",
                    "X-CSRF-Token": csrf_token,
                },
                json=payload,
                timeout=timeout,
                verify=verify_ssl,
            )
            create_response.raise_for_status()
        except requests.RequestException as err:
            raise SAPIntegrationError(f"SAP request failed: {err}") from err

        try:
            response_data = create_response.json()
        except ValueError as err:
            raise SAPIntegrationError("SAP returned an invalid JSON response") from err

    sap_data = response_data.get("d", response_data)
    business_partner = sap_data.get("BusinessPartner")
    if not business_partner:
        raise SAPIntegrationError(
            "SAP response did not contain a BusinessPartner value"
        )

    return str(business_partner)


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
            "SELECT vendor_id, email, vendor_legal_name FROM vendor WHERE vendor_id = %s",
            (vendor_id,),
        )
        vendor = cursor.fetchone()

        if not vendor:
            return jsonify({"error": "Vendor not found"}), 404

        if isapproved is True:
            password, password_hash = generate_unique_password(cursor)
            sap_vendor = create_sap_business_partner(vendor["vendor_legal_name"])
            cursor.execute(
                "UPDATE vendor SET status = %s, password = %s, sap_vendor = %s "
                "WHERE vendor_id = %s",
                ("active", password_hash, sap_vendor, vendor_id),
            )
            conn.commit()
            message = "Vendor approved successfully"
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
            response["sap_vendor"] = sap_vendor

        return jsonify(response), 200

    except SAPIntegrationError as err:
        if conn is not None:
            conn.rollback()
        return jsonify({"error": str(err)}), 502
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
    if not email:
        return jsonify({"error": "email is required"}), 400

    try:
        conn = get_connection()
        cursor = conn.cursor(dictionary=True)

        cursor.execute(
            "SELECT vendor_id FROM vendor WHERE email = %s",
            (email,),
        )
        vendor = cursor.fetchone()

        cursor.close()
        conn.close()

        if vendor:
            return jsonify({"message": "Email exists", "code": 200,"exists": True, "data": []}), 200
        else:
            return jsonify({"message": "Email not found", "code": 200, "exists": False, "data": []}), 200

    except MySQLError as err:
        return jsonify({"error": str(err)}), 500

@app.post("/vendors/check-pan")
def check_vendor_pan():
    data = request.get_json(silent=True) or {}

    pan = data.get("pan")
    if not pan:
        return jsonify({"error": "pan is required"}), 400
        
    try:
        conn = get_connection()
        cursor = conn.cursor(dictionary=True)

        cursor.execute(
            "SELECT vendor_id FROM vendor WHERE pan = %s",
            (pan,),
        )
        vendor = cursor.fetchone()

        cursor.close()
        conn.close()

        if vendor:
            return jsonify({"message": "PAN exists", "code": 200,"exists": True, "data": []}), 200
        else:
            return jsonify({"message": "PAN not found", "code": 200, "exists": False, "data": []}), 200

    except MySQLError as err:
        return jsonify({"error": str(err)}), 500


if __name__ == "__main__":
    app.run(debug=True)
