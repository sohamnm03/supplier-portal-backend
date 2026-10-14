import hashlib
import hmac
import json
import os
import re
import secrets
import uuid
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path
from urllib.parse import quote, unquote

import requests
from azure.core.exceptions import AzureError
from azure.storage.blob import BlobServiceClient, ContentSettings
from flask import Flask, g, jsonify, request
from flask_cors import CORS
from mysql.connector import Error as MySQLError

import auth
import rejections
import update_requests
from db import get_connection

app = Flask(__name__)
# Any local dev port: Vite moves to the next free port (5174, ...) when 5173 is already taken.
CORS(app, origins=[r"http://(localhost|127\.0\.0\.1):\d+"])

# Deployed shared-services API (email + invoice OCR). Override with SHARED_SERVICES_URL in .env if needed.
SHARED_SERVICES_URL = os.getenv("SHARED_SERVICES_URL", "https://fs-quad-shared.azurewebsites.net").rstrip("/")
EMAIL_API_URL = f"{SHARED_SERVICES_URL}/api/email/send"
INVOICE_OCR_API_URL = f"{SHARED_SERVICES_URL}/api/invoice/ocr"
EMAIL_VERIFICATION_API_URL = f"{SHARED_SERVICES_URL}/api/email-verification"

FRONTEND_BASE_URL = "http://localhost:5173/"
SUPPORT_EMAIL = "support@fourthsignal.com"
MAKER_EMAIL = "soham.m@fourthsignal.com"
REVIEW_DAYS = 3
MAKER_VENDOR_ID = "maker"
# The credentials email is the same template that used to go out when a checker approved a vendor (it already
# carries the username, password and login link).
SIGNUP_CREDENTIALS_TEMPLATE = "vendor_account_onboarding"
SOURCE_SELF_SERVICE = "self_service"
STATUS_REGISTERED = "registered"
STATUS_PENDING = "Pending"
IST =timezone(timedelta(hours=5, minutes=30))

VENDOR_FIELDS = [
    "title",
    "name",
    "email",
    "contact_no",
    "vendor_legal_name",
    "vendor_type",
    "year_established",
    "currency",
    "registration_number",
    "msme_status",
    "udyam_number",
    "assigned_rm",
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
    "source",
    "submission_round",
    "submitted_at",
    "returned_at",
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
    "email",
    "contact_no",
    "currency",
    "msme_status",
    "vendor_type",
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


MSME_STATUSES = {"Registered", "Not registered", "Not applicable"}
# Relationship managers a vendor can be assigned to (the same list as the portal's "Assigned RM" dropdown).
ASSIGNED_RMS = {"Priya Nair", "Arjun Verma", "Kavita Rao"}
VENDOR_TYPES = {
    "Individual",
    "Proprietorship",
    "Partnership",
    "Private limited company",
    "Public limited company",
    "Government entity",
    "Other",
}


def _clean_tax_id(value):
    """Strip stray leading punctuation OCR sometimes captures (e.g. ':29AAD...')."""
    if not isinstance(value, str):
        return value
    return value.strip().lstrip(":;.- 	").strip() or None


def _find_duplicate_identity(values, exclude_vendor_id=None):
    """Return a message if the PAN or email in `values` already belongs to another vendor."""
    checks = [
        ("pan", "PAN", lambda v: str(v).strip().upper(), "UPPER(TRIM(pan))"),
        ("email", "email", lambda v: str(v).strip().lower(), "LOWER(TRIM(email))"),
    ]
    conn = cursor = None
    try:
        conn = get_connection()
        cursor = conn.cursor(dictionary=True)
        for field, label, normalise, column_sql in checks:
            if not values.get(field) or not str(values[field]).strip():
                continue
            cursor.execute(
                # A rejected application no longer holds its email or PAN, so the vendor can apply again.
                f"SELECT vendor_id FROM vendor WHERE {column_sql} = %s AND vendor_id <> %s AND LOWER(status) <> 'rejected' LIMIT 1",
                (normalise(values[field]), exclude_vendor_id if exclude_vendor_id is not None else -1),
            )
            if cursor.fetchone():
                return f"A vendor with this {label} already exists."
    except MySQLError:
        return None  # the INSERT below will surface a real database problem
    finally:
        if cursor is not None:
            cursor.close()
        if conn is not None:
            conn.close()
    return None


def _validate_vendor_values(values, existing=None):
    """Return an error message for invalid vendor data, or None.

    `values` holds the fields being written; `existing` is the stored row (PATCH)
    so cross-field rules can see the final state.
    """
    for field in REQUIRED_FIELDS:
        if field in values and not str(values[field] or "").strip():
            return f"{field} cannot be empty"

    if values.get("msme_status") and values["msme_status"] not in MSME_STATUSES:
        return "msme_status must be one of: " + ", ".join(sorted(MSME_STATUSES))
    if values.get("vendor_type") and values["vendor_type"] not in VENDOR_TYPES:
        return "vendor_type is not a recognised value"
    if values.get("assigned_rm") and values["assigned_rm"] not in ASSIGNED_RMS:
        return "assigned_rm is not a recognised value"

    final = {**(existing or {}), **values}
    if final.get("msme_status") == "Registered" and not str(final.get("udyam_number") or "").strip():
        return "udyam_number is required when msme_status is Registered"
    # CIN is a 21-character code and the Udyam number column holds 30: anything else would only fail in the database.
    cin = str(final.get("cin") or "").strip()
    if cin and not re.fullmatch(r"[A-Za-z0-9]{21}", cin):
        return "cin must be a 21-character CIN"
    if len(str(final.get("udyam_number") or "").strip()) > 30:
        return "udyam_number is too long"
    return None


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


# SAP stores this value in the document's Reference field (BKPF-XBLNR), which is only 16
# characters wide — anything longer is silently cut off in SAP (FB03 showed every invoice as
# "INV-20261001-000"). The reference is therefore built to always fit in 16 characters, so the
# value shown in the app is exactly what SAP stores.
SAP_REFERENCE_MAX_LENGTH = 16


def _generate_invoice_reference(invoice_id):
    """Reference that traces an invoice from upload to its SAP posting:
    INV + YYMMDD + 7-digit invoice id (e.g. INV2610010000032), 16 characters."""
    return f"INV{datetime.now(IST).strftime('%y%m%d')}{int(invoice_id):07d}"


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


# No look-alike characters (0/O, 1/l/I), since the vendor types this password in from an email.
_PASSWORD_ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnpqrstuvwxyz23456789"


def generate_password():
    """A random 10-character password and its SHA-256 hash (the hash format login and the maker/checker app share)."""
    password = "".join(secrets.choice(_PASSWORD_ALPHABET) for _ in range(10))
    return password, hashlib.sha256(password.encode("utf-8")).hexdigest()


def _send_email(to, subject, template_name, params):
    """Call the shared email service. Never raises: a failed email must not fail the request."""
    try:
        requests.post(
            EMAIL_API_URL,
            json={
                "to": to,
                "subject": subject,
                "template_name": template_name,
                "params": params,
            },
            timeout=10,
        ).raise_for_status()
        return True
    except requests.RequestException as err:
        app.logger.warning("Email '%s' to %s failed: %s", template_name, to, err)
        return False


def _email_verification_headers():
    # Kept in one place: when the shared service switches authentication on, this becomes the API key header.
    return {"X-App-Id": "vss"}


def _email_verification_request(method, path, **kwargs):
    """Call the shared email-verification service. Returns the response, or None if it could not be reached."""
    try:
        return requests.request(
            method,
            f"{EMAIL_VERIFICATION_API_URL}{path}",
            headers=_email_verification_headers(),
            # Sending the code can take 20+ seconds on the shared service (it waits for the mail server).
            timeout=45,
            **kwargs,
        )
    except requests.RequestException as err:
        # Never log the request body: it carries the one-time code.
        app.logger.warning("Email verification request (%s) failed: %s", method, err)
        return None


def _email_verification_unavailable():
    return jsonify({"success": False, "message": "Email verification is unavailable right now. Please try again shortly."}), 503


def _relay_email_verification(upstream):
    """Pass the shared service's answer (body, status code, Retry-After) straight back to the browser."""
    if upstream is None:
        return _email_verification_unavailable()
    try:
        body = upstream.json()
    except ValueError:
        return jsonify({"success": False, "message": "Email verification returned an unexpected response."}), 502
    response = jsonify(body)
    response.status_code = upstream.status_code
    if upstream.headers.get("Retry-After"):
        response.headers["Retry-After"] = upstream.headers["Retry-After"]
    return response


def _verified_email_error(verification_id, email):
    """Return (message, status) unless `verification_id` is a verified verification of `email`.

    The shared service's status endpoint is the source of truth, so this is checked on the server whatever
    the browser claims.
    """
    if not isinstance(verification_id, str) or not verification_id.strip():
        return "Verify your email address before submitting.", 403

    upstream = _email_verification_request("GET", f"/{quote(verification_id.strip(), safe='')}/status")
    if upstream is None:
        return "Email verification is unavailable right now. Please try again shortly.", 503
    if upstream.status_code in (400, 404):
        return "Verify your email address before submitting.", 403
    if not upstream.ok:
        return "Email verification could not be confirmed. Please try again.", 502

    try:
        body = upstream.json()
    except ValueError:
        return "Email verification could not be confirmed. Please try again.", 502

    verified_email = str(body.get("email") or "").strip().lower()
    if body.get("status") != "verified" or verified_email != str(email or "").strip().lower():
        return "Verify your email address before submitting.", 403
    return None


def _now_ist_text():
    return datetime.now(IST).strftime("%d %b %Y, %I:%M %p IST")


def _send_registration_emails(vendor_id, vendor_name, vendor_email, resubmission=False):
    request_id = f"VR-{vendor_id}"
    _send_email(
        vendor_email,
        "We've received your updated application" if resubmission else "We've received your registration request",
        "vendor_registration_received",
        {
            "vendor_name": vendor_name,
            "review_days": REVIEW_DAYS,
            "request_id": request_id,
            "support_email": SUPPORT_EMAIL,
        },
    )
    _send_email(
        MAKER_EMAIL,
        f"Vendor application resubmitted - {vendor_name}" if resubmission else f"New vendor registration awaiting review - {vendor_name}",
        "vendor_registration_submitted",
        {
            "vendor_name": vendor_name,
            "submitted_by": f"{vendor_name} ({vendor_email})",
            "submitted_at": _now_ist_text(),
            "request_id": request_id,
            "review_url": FRONTEND_BASE_URL,
        },
    )


def _send_invoice_emails(vendor_name, vendor_email, invoice_number, total_amount, currency):
    amount_text = f"{currency or 'INR'} {total_amount:,.2f}" if total_amount is not None else "N/A"
    _send_email(
        vendor_email,
        f"Invoice received - {invoice_number}",
        "invoice_received",
        {
            "vendor_name": vendor_name,
            "invoice_number": invoice_number,
            "invoice_amount": amount_text,
            "review_days": REVIEW_DAYS,
            "support_email": SUPPORT_EMAIL,
        },
    )
    _send_email(
        MAKER_EMAIL,
        f"New invoice needs review - {invoice_number}",
        "invoice_submitted",
        {
            "vendor_name": vendor_name,
            "invoice_number": invoice_number,
            "invoice_amount": amount_text,
            "submitted_at": _now_ist_text(),
            "review_url": FRONTEND_BASE_URL,
        },
    )


@app.get("/vendors")
def get_vendors():
    # Vendor records are personal data and signup is now open to anyone, so vendors are no longer listed or
    # looked up publicly. The portal reads its own record from GET /vendors/me.
    return jsonify({"error": "Not available. Use GET /vendors/me."}), 404


# --- Self-service signup, onboarding form and progress ------------------------------------------------------
#
# Lifecycle (vendor.status):  registered -> Pending (maker) -> 01 (checker) -> 02 -> active
#                                  ^                |
#                                  +---- returned <-+   (maker or checker sends it back with reasons)
# A vendor signs up with a verified email + phone and is emailed credentials straight away. They log in, fill
# in the rest of the form (saved as a draft), and submit; maker and checker then review exactly as for any
# new vendor. A return puts the vendor back in the form with the flagged fields highlighted; resubmitting
# starts the maker -> checker review again.

# Form fields a vendor fills in during onboarding. The email is the login name and is fixed at signup.
ONBOARDING_FIELDS = [
    "vendor_legal_name", "contact_no", "vendor_type", "year_established", "currency",
    "registration_number", "msme_status", "udyam_number", "assigned_rm", "gstin", "pan", "aadhaar_no", "cin",
    "street", "city", "district", "region", "postal_code",
    "account_holder_name", "bank_name", "branch_name", "bank_account_no", "ifsc_code",
]
EMAIL_PATTERN = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
PHONE_PATTERN = re.compile(r"^\+?[0-9][0-9 \-]{6,18}$")


def _vendor_with_progress(vendor_id):
    """The vendor's own record plus what the portal needs to show: lifecycle stage and any open return."""
    conn = cursor = None
    try:
        conn = get_connection()
        cursor = conn.cursor(dictionary=True)
        cursor.execute(
            f"SELECT {', '.join(VENDOR_RESPONSE_FIELDS)} FROM vendor WHERE vendor_id = %s LIMIT 1", (vendor_id,)
        )
        row = cursor.fetchone()
    finally:
        if cursor is not None:
            cursor.close()
        if conn is not None:
            conn.close()
    if row is None:
        return None

    vendor = _serialize_row(row)
    vendor["stage"] = auth.stage_for(vendor.get("status"), vendor.get("submission_round"))
    vendor["rejection"] = (
        rejections.get_open_rejection(vendor_id) if vendor["stage"] in (auth.STAGE_RETURNED, auth.STAGE_REJECTED) else None
    )
    # The rejecter's name is internal; the vendor only learns whether it was the maker's or checker's review.
    return vendor


@app.post("/vendors/signup")
def signup_vendor():
    """Create a vendor account from a verified email and a phone number, and email the login credentials."""
    data = request.get_json(silent=True) or {}
    email = str(data.get("email") or "").strip()
    contact_no = str(data.get("contact_no") or "").strip()

    if not EMAIL_PATTERN.match(email) or len(email) > 100:
        return jsonify({"error": "Enter a valid email address."}), 400
    if not PHONE_PATTERN.match(contact_no) or len(contact_no) > 20:
        return jsonify({"error": "Enter a valid phone number."}), 400

    # The email must have been verified through the shared service for this very address.
    verification_error = _verified_email_error(data.get("verification_id"), email)
    if verification_error:
        return jsonify({"error": verification_error[0]}), verification_error[1]

    if _find_duplicate_identity({"email": email}):
        return jsonify({
            "error": "An account with this email already exists. Please sign in instead.",
            "code": "email_exists",
        }), 409

    password, password_hash = generate_password()
    conn = cursor = None
    try:
        conn = get_connection()
        cursor = conn.cursor(dictionary=True)
        cursor.execute(
            "INSERT INTO vendor (email, contact_no, status, source, registered_at, password) "
            "VALUES (%s, %s, %s, %s, NOW(), %s)",
            (email, contact_no, STATUS_REGISTERED, SOURCE_SELF_SERVICE, password_hash),
        )
        vendor_id = cursor.lastrowid
        # A vendor whose earlier application was rejected is applying again: that application stays as history, but its
        # login is retired so the new account is the only one this email can sign in to.
        cursor.execute(
            "UPDATE vendor SET password = NULL WHERE LOWER(TRIM(email)) = %s AND LOWER(status) = 'rejected' AND vendor_id <> %s",
            (email.lower(), vendor_id),
        )

        # Send the credentials before committing: if the email cannot go out the vendor would have an account
        # they can never get into, so the signup is rolled back and they can simply try again.
        if not _send_email(
            email,
            "Your Vendor Central account is ready",
            SIGNUP_CREDENTIALS_TEMPLATE,
            {
                "vendor_name": "Vendor",  # required by the older template; the dedicated one ignores it
                "username": email,
                "password": password,
                "login_url": FRONTEND_BASE_URL,
                "support_email": SUPPORT_EMAIL,
            },
        ):
            conn.rollback()
            return jsonify({"error": "We couldn't send your login details. Please try again in a moment."}), 502
        conn.commit()
    except MySQLError as err:
        if conn is not None:
            conn.rollback()
        return jsonify({"error": str(err)}), 500
    finally:
        if cursor is not None:
            cursor.close()
        if conn is not None:
            conn.close()

    return jsonify({"message": "Account created. Your login details have been emailed to you.", "vendor_id": vendor_id}), 201


@app.get("/vendors/me")
@auth.vendor_required()
def get_my_vendor():
    vendor = _vendor_with_progress(g.vendor["vendor_id"])
    if vendor is None:
        return jsonify({"error": "Vendor not found"}), 404
    return jsonify(vendor), 200


def _onboarding_values(data):
    """The editable onboarding fields present in `data`, cleaned."""
    values = {}
    for field in ONBOARDING_FIELDS:
        if field not in data:
            continue
        value = data[field]
        values[field] = value.strip() if isinstance(value, str) else value
        if values[field] == "":
            values[field] = None
    if "msme_status" in values and values["msme_status"] != "Registered":
        values["udyam_number"] = None
    if isinstance(values.get("cin"), str):
        values["cin"] = values["cin"].upper()
    return values


def _save_onboarding(vendor_id, vendor_row, values):
    """Write `values` to the vendor row and tell the open return which flagged fields the vendor has now changed."""
    if values:
        assignments = ", ".join(f"{field} = %s" for field in values)
        # `name` mirrors the legal name (the enterprise app and SAP read it).
        if "vendor_legal_name" in values:
            assignments += ", name = %s"
        params = [*values.values()]
        if "vendor_legal_name" in values:
            params.append(values["vendor_legal_name"])
        conn = get_connection()
        try:
            cursor = conn.cursor()
            cursor.execute(f"UPDATE vendor SET {assignments} WHERE vendor_id = %s", [*params, vendor_id])
            conn.commit()
        finally:
            conn.close()
    changed = [field for field, value in values.items() if _norm(value) != _norm(vendor_row.get(field))]
    rejections.mark_fixed(vendor_id, changed)


def _load_vendor_row(vendor_id):
    conn = get_connection()
    try:
        cursor = conn.cursor(dictionary=True)
        cursor.execute("SELECT * FROM vendor WHERE vendor_id = %s", (vendor_id,))
        return cursor.fetchone()
    finally:
        conn.close()


@app.put("/vendors/me/draft")
@auth.vendor_required(auth.STAGE_DRAFT, auth.STAGE_RETURNED)
def save_my_draft():
    """Save the onboarding form as it stands (partial is fine). Only the values sent are written."""
    data = request.get_json(silent=True)
    if not isinstance(data, dict):
        return jsonify({"error": "Request body must be JSON"}), 400

    vendor_id = g.vendor["vendor_id"]
    values = _onboarding_values(data)
    # Drafts may be incomplete, so only the values actually provided are validated.
    provided = {field: value for field, value in values.items() if value is not None}
    validation_error = _validate_vendor_values(provided)
    if validation_error:
        return jsonify({"error": validation_error}), 400
    duplicate_error = _find_duplicate_identity({"pan": values.get("pan")}, exclude_vendor_id=vendor_id)
    if duplicate_error:
        return jsonify({"error": duplicate_error}), 409

    try:
        _save_onboarding(vendor_id, _load_vendor_row(vendor_id), values)
        return jsonify(_vendor_with_progress(vendor_id)), 200
    except MySQLError as err:
        return jsonify({"error": str(err)}), 500


@app.post("/vendors/me/submit")
@auth.vendor_required(auth.STAGE_DRAFT, auth.STAGE_RETURNED)
def submit_my_application():
    """Submit (or resubmit after a return) the application to the maker. Review then runs as for any new vendor."""
    data = request.get_json(silent=True)
    if not isinstance(data, dict):
        return jsonify({"error": "Request body must be JSON"}), 400

    vendor_id = g.vendor["vendor_id"]
    try:
        before = _load_vendor_row(vendor_id)
        values = _onboarding_values(data.get("details") or {})
        final = {**{field: before.get(field) for field in ONBOARDING_FIELDS}, **values}
        final["email"] = before["email"]

        missing = [field for field in REQUIRED_FIELDS if not str(final.get(field) or "").strip()]
        if missing:
            return jsonify({"error": f"Missing required field(s): {', '.join(missing)}", "fields": missing}), 400
        validation_error = _validate_vendor_values({k: v for k, v in final.items() if v is not None})
        if validation_error:
            return jsonify({"error": validation_error}), 400
        duplicate_error = _find_duplicate_identity({"pan": final.get("pan")}, exclude_vendor_id=vendor_id)
        if duplicate_error:
            return jsonify({"error": duplicate_error}), 409

        _save_onboarding(vendor_id, before, values)

        # A flagged field the vendor checked and kept as it was (the rejection may have been about a document,
        # or the value was right) still counts as dealt with.
        confirmed = data.get("confirmed_fields")
        if isinstance(confirmed, list):
            rejections.mark_fixed(vendor_id, [str(key) for key in confirmed])

        conn = get_connection()
        try:
            cursor = conn.cursor()
            cursor.execute(
                "UPDATE vendor SET status = %s, submission_round = submission_round + 1, "
                "submitted_at = NOW(), is_draft = 0 WHERE vendor_id = %s",
                (STATUS_PENDING, vendor_id),
            )
            conn.commit()
        finally:
            conn.close()
        rejections.mark_resubmitted(vendor_id)
        vendor = _vendor_with_progress(vendor_id)
    except MySQLError as err:
        return jsonify({"error": str(err)}), 500

    _send_registration_emails(
        vendor_id,
        vendor.get("vendor_legal_name") or vendor.get("name"),
        vendor["email"],
        resubmission=(vendor.get("submission_round") or 0) > 1,
    )
    return jsonify(vendor), 200


@app.post("/vendors")
def create_vendor():
    # Replaced by POST /vendors/signup + the onboarding form (PUT /vendors/me/draft, POST /vendors/me/submit).
    return jsonify({"error": "Vendor requests now start with signup. Use POST /vendors/signup."}), 410


# Supporting documents uploaded with an onboarding request, kept in the Azure Blob container named by
# AZURE_DOCS_BLOB_CONTAINER (no database table needed):
#   vendor_<id>/<type>_<random>.<ext>, with the original file name, type and upload time as blob metadata.
# Each type maps to how many files it accepts. Keep this in step with the portal's "Upload supporting documents".
DOCUMENT_TYPES = {
    "pan": ("PAN card", 1),
    "aadhaar": ("Aadhaar card", 1),
    "gstin": ("GST registration certificate", 1),
    "bank": ("Bank account proof", 1),
    "cin": ("CIN / certificate of incorporation", 1),
    "other": ("Other supporting document", 3),
}
DOCUMENT_MAX_BYTES = 5 * 1024 * 1024
DOCUMENT_MAX_PER_VENDOR = 16
# A file must actually start the way its extension says, so a renamed .exe is not accepted as a PDF.
DOCUMENT_SIGNATURES = {
    ".pdf": b"%PDF",
    ".png": b"\x89PNG\r\n\x1a\n",
    ".jpg": b"\xff\xd8\xff",
    ".jpeg": b"\xff\xd8\xff",
}
DOCUMENT_CONTENT_TYPES = {
    ".pdf": "application/pdf",
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
}


def _docs_container():
    """The supporting-documents blob container, or None when Azure storage is not configured."""
    connection_string = os.getenv("AZURE_STORAGE_CONNECTION_STRING")
    container_name = os.getenv("AZURE_DOCS_BLOB_CONTAINER")
    if not connection_string or not container_name:
        return None
    return BlobServiceClient.from_connection_string(connection_string).get_container_client(container_name)


@app.post("/vendors/<int:vendor_id>/documents")
@auth.vendor_required(auth.STAGE_DRAFT, auth.STAGE_RETURNED)
def upload_vendor_documents(vendor_id):
    conn = cursor = None
    try:
        conn = get_connection()
        cursor = conn.cursor(dictionary=True)
        cursor.execute("SELECT vendor_id, status FROM vendor WHERE vendor_id = %s", (vendor_id,))
        vendor = cursor.fetchone()
    except MySQLError as err:
        return jsonify({"error": str(err)}), 500
    finally:
        if cursor is not None:
            cursor.close()
        if conn is not None:
            conn.close()

    if vendor is None:
        return jsonify({"error": "Vendor not found"}), 404

    unknown = [key for key in request.files if key not in DOCUMENT_TYPES]
    if unknown:
        return jsonify({"error": "Unknown document type: " + ", ".join(unknown)}), 400

    # Check every file before saving any, so a bad one never leaves a half-uploaded set behind.
    staged = []
    for doc_type, (label, max_files) in DOCUMENT_TYPES.items():
        files = [item for item in request.files.getlist(doc_type) if item and item.filename]
        if len(files) > max_files:
            return jsonify({"error": f"{label}: attach at most {max_files} file(s)."}), 400
        for item in files:
            extension = Path(item.filename).suffix.lower()
            if extension not in DOCUMENT_SIGNATURES:
                return jsonify({"error": f"{label}: only PDF, JPG or PNG files are accepted."}), 400
            content = item.stream.read(DOCUMENT_MAX_BYTES + 1)
            if len(content) > DOCUMENT_MAX_BYTES:
                return jsonify({"error": f"{label}: each file must be 5 MB or smaller."}), 400
            if not content.startswith(DOCUMENT_SIGNATURES[extension]):
                return jsonify({"error": f"{label}: {item.filename} is not a valid {extension[1:].upper()} file."}), 400
            staged.append((doc_type, item.filename, extension, content))

    if not staged:
        return jsonify({"error": "No documents were attached."}), 400

    container = _docs_container()
    if container is None:
        return jsonify({"error": "Document storage is not configured."}), 503

    prefix = f"vendor_{vendor_id}/"
    uploaded_blobs = []
    saved = []
    try:
        existing_blobs = list(container.list_blobs(name_starts_with=prefix))
        # Uploading a document type replaces what was on file for it (a vendor fixing a returned application).
        replaced_prefixes = {f"{prefix}{doc_type}_" for doc_type, *_ in staged}
        replaced_blobs = [blob.name for blob in existing_blobs if blob.name.startswith(tuple(replaced_prefixes))]
        if len(existing_blobs) - len(replaced_blobs) + len(staged) > DOCUMENT_MAX_PER_VENDOR:
            return jsonify({"error": "This request already has the maximum number of documents."}), 400

        for doc_type, original_name, extension, content in staged:
            blob_name = f"{prefix}{doc_type}_{uuid.uuid4().hex[:12]}{extension}"
            uploaded_at = _now_ist_text()
            container.upload_blob(
                name=blob_name,
                data=content,
                overwrite=False,
                content_settings=ContentSettings(content_type=DOCUMENT_CONTENT_TYPES[extension]),
                # Blob metadata must be ASCII, so the original file name is percent-encoded.
                metadata={
                    "vendor_id": str(vendor_id),
                    "doc_type": doc_type,
                    "original_name": quote(original_name[:255], safe=""),
                    "uploaded_at": uploaded_at,
                },
            )
            uploaded_blobs.append(blob_name)
            saved.append({
                "type": doc_type,
                "original_name": original_name[:255],
                "blob_name": blob_name,
                "size": len(content),
                "uploaded_at": uploaded_at,
            })
    except AzureError:
        app.logger.exception("Uploading supporting documents for vendor %s failed", vendor_id)
        # Do not leave half a set behind: remove whatever this request already stored.
        for blob_name in uploaded_blobs:
            try:
                container.delete_blob(blob_name)
            except AzureError:
                app.logger.exception("Could not remove %s after a failed upload", blob_name)
        return jsonify({"error": "Unable to save the documents. Please try again."}), 502

    for blob_name in replaced_blobs:
        try:
            container.delete_blob(blob_name)
        except AzureError:
            app.logger.exception("Could not remove the replaced document %s", blob_name)

    # A replaced document answers a "doc_<type>" flag on the open return.
    rejections.mark_fixed(vendor_id, [f"doc_{item['type']}" for item in saved])
    return jsonify({"documents": saved}), 201


@app.get("/vendors/me/documents")
@auth.vendor_required(auth.STAGE_DRAFT, auth.STAGE_RETURNED)
def list_my_documents():
    """The supporting documents already on file, so the form can show them and offer to replace them."""
    vendor_id = g.vendor["vendor_id"]
    container = _docs_container()
    if container is None:
        return jsonify({"documents": [], "storage_configured": False}), 200
    documents = []
    try:
        for blob in container.list_blobs(name_starts_with=f"vendor_{vendor_id}/", include=["metadata"]):
            metadata = blob.metadata or {}
            documents.append({
                "type": metadata.get("doc_type"),
                "original_name": unquote(metadata.get("original_name") or blob.name.rsplit("/", 1)[-1]),
                "size": blob.size,
                "uploaded_at": metadata.get("uploaded_at"),
            })
    except AzureError:
        app.logger.exception("Listing documents for vendor %s failed", vendor_id)
        return jsonify({"error": "Unable to load your documents right now."}), 502
    return jsonify({"documents": documents, "storage_configured": True}), 200


# Profile fields a vendor may ask to change. Email is the login name, so it is not editable here.
PROFILE_FIELDS = [
    "vendor_legal_name", "contact_no", "vendor_type", "year_established", "currency",
    "registration_number", "msme_status", "udyam_number", "gstin", "pan", "aadhaar_no", "cin",
    "street", "city", "district", "region", "postal_code",
    "account_holder_name", "bank_name", "branch_name", "bank_account_no", "ifsc_code",
]


# Details that come from the GSTIN / PAN registration and are therefore fixed once set: the portal greys
# them out, and the server refuses a request that tries to change them anyway.
LOCKED_PROFILE_FIELDS = ["gstin", "pan", "vendor_legal_name", "street", "region", "postal_code"]


def _norm(value):
    return "" if value is None else str(value).strip()


@app.patch("/vendors")
def update_vendor():
    # Profile changes must go through the review flow (POST /vendors/update-requests).
    return jsonify({"error": "Vendor details can no longer be changed directly. Submit an update request instead."}), 403


@app.post("/vendors/update-requests")
@auth.vendor_required(auth.STAGE_ACTIVE)
def create_update_request():
    data = request.get_json(silent=True) or {}
    vendor_id = data.get("vendor_id")
    details = data.get("details")
    if vendor_id is None or not isinstance(details, dict):
        return jsonify({"error": "vendor_id and details are required"}), 400

    conn = cursor = None
    try:
        conn = get_connection()
        cursor = conn.cursor(dictionary=True)
        cursor.execute("SELECT * FROM vendor WHERE vendor_id = %s", (vendor_id,))
        vendor = cursor.fetchone()
    except MySQLError as err:
        return jsonify({"error": str(err)}), 500
    finally:
        if cursor is not None:
            cursor.close()
        if conn is not None:
            conn.close()

    if vendor is None:
        return jsonify({"error": "Vendor not found"}), 404
    if _norm(vendor.get("status")).lower() != "active":
        return jsonify({"error": "Only active vendors can request profile updates"}), 409

    if update_requests.get_open_request(vendor_id):
        return jsonify({"error": "You already have an update request awaiting review."}), 409

    requested = {field: details[field] for field in PROFILE_FIELDS if field in details}
    if requested.get("msme_status") != "Registered" and "msme_status" in requested:
        requested["udyam_number"] = None
    validation_error = _validate_vendor_values(requested, vendor)
    if validation_error:
        return jsonify({"error": validation_error}), 400

    if not _norm(requested.get("gstin", vendor.get("gstin"))):
        return jsonify({"error": "GSTIN is required"}), 400

    changed = [f for f in requested if _norm(requested[f]) != _norm(vendor.get(f))]
    locked = [f for f in changed if f in LOCKED_PROFILE_FIELDS and _norm(vendor.get(f))]
    if locked:
        return jsonify({"error": "These details can't be changed: " + ", ".join(locked)}), 400
    if not changed:
        return jsonify({"error": "No changes to submit."}), 400

    old_values = {f: vendor.get(f) for f in changed}
    new_values = {f: requested[f] for f in changed}
    row = update_requests.create_request(vendor_id, old_values, new_values, changed, vendor.get("email"))

    # Same acknowledgement + "awaiting review" emails a new registration triggers, reused until
    # dedicated update-request templates exist. _send_email never raises.
    if vendor.get("email"):
        vendor_name = vendor.get("vendor_legal_name") or vendor.get("name")
        request_ref = f"UR-{row['request_id']}"
        _send_email(
            vendor["email"],
            "We've received your update request",
            "vendor_registration_received",
            {"vendor_name": vendor_name, "review_days": REVIEW_DAYS, "request_id": request_ref, "support_email": SUPPORT_EMAIL},
        )
        _send_email(
            MAKER_EMAIL,
            f"Vendor update request awaiting review - {vendor_name}",
            "vendor_registration_submitted",
            {
                "vendor_name": vendor_name,
                "submitted_by": f"{vendor_name} ({vendor['email']})",
                "submitted_at": _now_ist_text(),
                "request_id": request_ref,
                "review_url": FRONTEND_BASE_URL,
            },
        )
    return jsonify(row), 201


@app.get("/vendors/update-requests")
@auth.vendor_required(auth.STAGE_ACTIVE)
def list_update_requests():
    vendor_id = request.args.get("vendor_id")
    if not vendor_id:
        return jsonify({"error": "vendor_id is required"}), 400
    return jsonify(update_requests.list_requests(int(vendor_id))), 200


@app.post("/vendors/approval")
def approve_vendor():
    # Approval belongs to the maker/checker app, which also creates the SAP vendor. Vendors now get their
    # credentials at signup, so this open endpoint (which used to issue a new password) is retired.
    return jsonify({"error": "Vendor approval is done in the maker/checker app."}), 410


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
            "SELECT vendor_id FROM vendor WHERE LOWER(TRIM(email)) = %s AND LOWER(status) <> 'rejected' LIMIT 1",
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


@app.post("/vendors/email-verification/send")
def send_email_verification():
    data = request.get_json(silent=True) or {}
    email = data.get("email")
    if not isinstance(email, str) or not email.strip():
        return jsonify({"success": False, "message": "email is required"}), 400

    payload = {"email": email.strip()}
    if isinstance(data.get("recipient_name"), str) and data["recipient_name"].strip():
        payload["recipient_name"] = data["recipient_name"].strip()
    return _relay_email_verification(_email_verification_request("POST", "/send", json=payload))


@app.post("/vendors/email-verification/verify-otp")
def verify_email_otp():
    data = request.get_json(silent=True) or {}
    verification_id = data.get("verification_id")
    otp = data.get("otp")
    if not isinstance(verification_id, str) or not verification_id.strip() or not isinstance(otp, str):
        return jsonify({"success": False, "message": "verification_id and otp are required"}), 400

    return _relay_email_verification(
        _email_verification_request("POST", "/verify-otp", json={"verification_id": verification_id.strip(), "otp": otp.strip()})
    )


@app.get("/vendors/email-verification/<verification_id>/status")
def email_verification_status(verification_id):
    return _relay_email_verification(_email_verification_request("GET", f"/{quote(verification_id, safe='')}/status"))


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

    wait = auth.login_blocked_for(email)
    if wait:
        response = jsonify({"error": "Too many failed attempts. Please try again in a few minutes."})
        response.status_code = 429
        response.headers["Retry-After"] = str(wait)
        return response

    conn = None
    cursor = None
    try:
        conn = get_connection()
        cursor = conn.cursor(dictionary=True)
        # Any vendor with credentials can sign in; what they can do afterwards depends on their stage.
        cursor.execute(
            "SELECT vendor_id, email, password, status, submission_round FROM vendor "
            "WHERE LOWER(TRIM(email)) = %s AND password IS NOT NULL "
            "ORDER BY (LOWER(status) = 'rejected'), vendor_id DESC LIMIT 1",
            (email.strip().lower(),),
        )
        vendor = cursor.fetchone()

        submitted_hash = hashlib.sha256(password.encode("utf-8")).hexdigest()
        stored_hash = vendor.get("password") if vendor else None
        if not stored_hash or not hmac.compare_digest(str(stored_hash), submitted_hash):
            auth.record_login_failure(email)
            return jsonify({"error": "Invalid email or password"}), 401

        auth.clear_login_failures(email)
        stage = auth.stage_for(vendor["status"], vendor.get("submission_round"))
        if stage == auth.STAGE_REJECTED:
            # No session for a rejected application. The vendor is told why and can start a new application.
            return jsonify({"error": "Your application was not approved.", "code": "application_rejected"}), 403
        return jsonify(
            {
                "message": "Login successful",
                "vendor_id": vendor["vendor_id"],
                "email": vendor["email"],
                "status": vendor["status"],
                "stage": stage,
                "token": auth.issue_token(vendor["vendor_id"], vendor["email"]),
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

        # A vendor filling in their own form must not be told their own saved PAN is a duplicate.
        own_id = auth.current_vendor_id()
        cursor.execute(
            "SELECT vendor_id FROM vendor WHERE UPPER(TRIM(pan)) = %s AND vendor_id <> %s AND LOWER(status) <> 'rejected' LIMIT 1",
            (normalized_pan, own_id if own_id is not None else -1),
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
@auth.vendor_required(auth.STAGE_ACTIVE)
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
            "SELECT vendor_id, email, vendor_legal_name FROM vendor "
            "WHERE vendor_id = %s LIMIT 1",
            (vendor_id,),
        )
        vendor_row = validation_cursor.fetchone()
        if vendor_row is None:
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
        "vendor_tax_id": _clean_tax_id(invoice_data.get("vendor_tax_id")),
        "vendor_address": invoice_data.get("vendor_address"),
        "vendor_address_recipient": invoice_data.get("vendor_address_recipient"),
        "customer_id": invoice_data.get("customer_id"),
        "customer_name": invoice_data.get("customer_name"),
        "customer_tax_id": _clean_tax_id(invoice_data.get("customer_tax_id")),
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
        # System-generated reference, same format the enterprise app uses: INV<YYMMDD><7-digit invoice id>.
        cursor.execute(
            "UPDATE invoices SET inv_ref_gen = %s WHERE id = %s",
            (_generate_invoice_reference(database_invoice_id), database_invoice_id),
        )

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

    if vendor_row.get("email"):
        _send_invoice_emails(
            vendor_row.get("vendor_legal_name"),
            vendor_row["email"],
            invoice_number or invoice_file.filename,
            invoice_values["total_amount"],
            invoice_values["currency"],
        )

    return jsonify(
        {
            "message": "Invoice processed and saved successfully",
            "invoice_id": database_invoice_id,
            "file_id": file_id,
            "ocr_response": ocr_data,
        }
    ), 201


@app.get("/invoices")
@auth.vendor_required(auth.STAGE_ACTIVE)
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
            "SELECT gstin FROM vendor WHERE vendor_id = %s LIMIT 1",
            (vendor_id,),
        )
        vendor_row = cursor.fetchone()
        vendor_gstin = ((vendor_row or {}).get("gstin") or "").strip().upper()

        # Invoices uploaded by the vendor itself, plus invoices a maker uploaded
        # on the vendor's behalf (stored with vendor_id = 'maker', matched by GSTIN).
        if vendor_gstin:
            cursor.execute(
                "SELECT * FROM invoices WHERE vendor_id = %s "
                "OR (vendor_id = %s AND UPPER(TRIM(vendor_tax_id)) = %s) "
                "ORDER BY id",
                (vendor_id, MAKER_VENDOR_ID, vendor_gstin),
            )
        else:
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
            serialized_invoice["uploaded_by_type"] = (
                "maker" if invoice["vendor_id"] == MAKER_VENDOR_ID else "vendor"
            )
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
