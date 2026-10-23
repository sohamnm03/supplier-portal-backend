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
from flask import Flask, Response, g, jsonify, request
from flask_cors import CORS
from mysql.connector import Error as MySQLError

import agreement_hold
import agreements
import auth
import invoice_versions
import rejections
import update_requests
import verification
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
# Where the business team reviews invoices (the enterprise app), for the link in the MSME invoice email.
ENTERPRISE_APP_URL = os.getenv("ENTERPRISE_APP_URL", FRONTEND_BASE_URL)
# Invoice from an MSME vendor: emailed to the groups wired to this event on the enterprise app's Configure Emails screen.
MSME_INVOICE_TEMPLATE = "invoice_msme_received"
MSME_EVENT_CODE = "INVOICE_MSME_RECEIVED"
MSME_PAYMENT_DAYS = 45
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
    "correction_fields",
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


# Who holds a PAN or an email. A PAN is claimed only by a SUBMITTED application: not a maker's saved draft (is_draft = 1), not a vendor
# who has only signed up and is still filling the form ('registered'), not a rejected one. The email is the login, so any real account
# holds it (a signed-up 'registered' vendor included), but not a maker's draft or a rejected application.
PAN_CLAIMED_SQL = "is_draft = 0 AND LOWER(status) NOT IN ('registered', 'rejected')"
EMAIL_HELD_SQL = "is_draft = 0 AND LOWER(status) <> 'rejected'"


def pan_duplicate_check_enabled():
    """Whether a PAN already held by a submitted vendor is refused (VENDOR_PAN_DUPLICATE_CHECK: true / 1 / yes / on).

    TODO: re-enable after testing. It is OFF by default for now (testing and demos reuse one vendor's PAN); the email checks, the PAN / GSTIN /
    Aadhaar verification and the PAN-belongs-to-GSTIN checks are not affected by this switch."""
    return (os.getenv("VENDOR_PAN_DUPLICATE_CHECK") or "").strip().lower() in ("1", "true", "yes", "on")


def _find_duplicate_identity(values, exclude_vendor_id=None):
    """Return a message if the PAN or email in `values` already belongs to another vendor."""
    checks = [
        ("pan", "PAN", lambda v: str(v).strip().upper(), "UPPER(TRIM(pan))", PAN_CLAIMED_SQL),
        ("email", "email", lambda v: str(v).strip().lower(), "LOWER(TRIM(email))", EMAIL_HELD_SQL),
    ]
    conn = cursor = None
    try:
        conn = get_connection()
        cursor = conn.cursor(dictionary=True)
        for field, label, normalise, column_sql, holds_sql in checks:
            if not values.get(field) or not str(values[field]).strip():
                continue
            if field == "pan" and not pan_duplicate_check_enabled():
                continue  # TODO: re-enable after testing
            cursor.execute(
                f"SELECT vendor_id FROM vendor WHERE {column_sql} = %s AND vendor_id <> %s AND {holds_sql} LIMIT 1",
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


def _validate_vendor_values(values, existing=None, draft=False):
    """Return an error message for invalid vendor data, or None.

    `draft`: saving progress, not submitting. Only what would break the database is refused (the CIN / Udyam lengths); the business
    rules (required, allowed values, Udyam when registered) are enforced when the application is submitted.

    `values` holds the fields being written; `existing` is the stored row (PATCH)
    so cross-field rules can see the final state.
    """
    for field in REQUIRED_FIELDS:
        if not draft and field in values and not str(values[field] or "").strip():
            return f"{field} cannot be empty"

    if not draft and values.get("msme_status") and values["msme_status"] not in MSME_STATUSES:
        return "msme_status must be one of: " + ", ".join(sorted(MSME_STATUSES))
    if not draft and values.get("vendor_type") and values["vendor_type"] not in VENDOR_TYPES:
        return "vendor_type is not a recognised value"
    if not draft and values.get("assigned_rm") and values["assigned_rm"] not in ASSIGNED_RMS:
        return "assigned_rm is not a recognised value"

    final = {**(existing or {}), **values}
    if not draft and final.get("msme_status") == "Registered" and not str(final.get("udyam_number") or "").strip():
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


def _send_email(to, subject, template_name, params, cc=None):
    """Call the shared email service. Never raises: a failed email must not fail the request."""
    body = {
        "to": to,
        "subject": subject,
        "template_name": template_name,
        "params": params,
    }
    if cc:
        body["cc"] = cc
    try:
        requests.post(EMAIL_API_URL, json=body, timeout=10).raise_for_status()
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


def _msme_category(msme_status):
    """The category the MSME email shows, or None when the vendor is not an MSME. The portal's own status list is only
    Registered / Not registered / Not applicable (no Micro / Small / Medium), so a "Registered" vendor has no category to give:
    that is sent as "Not specified" rather than guessed. A tier recorded by the enterprise app's form is passed through."""
    status = str(msme_status or "").strip().lower()
    if status in ("micro", "small", "medium"):
        return status.capitalize()
    if status == "registered":
        return "Not specified"
    return None


def _format_amount(value, currency):
    """₹12,50,000.00 for INR (lakh / crore grouping), "USD 1,250,000.00" otherwise: the MSME email shows this verbatim."""
    amount = Decimal(str(value)).quantize(Decimal("0.01"))
    whole, _, fraction = f"{abs(amount):.2f}".partition(".")
    code = (currency or "INR").upper()
    if code == "INR" and len(whole) > 3:
        head, tail, groups = whole[:-3], whole[-3:], []
        while len(head) > 2:
            groups.insert(0, head[-2:])
            head = head[:-2]
        whole = ",".join([head, *groups, tail])
    elif code != "INR":
        whole = f"{int(whole):,}"
    return f"{'-' if amount < 0 else ''}{'₹' if code == 'INR' else code + ' '}{whole}.{fraction}"


def _msme_recipients():
    """Who gets the MSME invoice email: the groups wired to the INVOICE_MSME_RECEIVED event on the enterprise app's
    Configure Emails screen (same database), so one screen controls both apps. Falls back to the maker when the event is not
    set up yet or the lookup fails, so the alert is never silently dropped."""
    conn = cursor = None
    try:
        conn = get_connection()
        cursor = conn.cursor()
        cursor.execute(
            "SELECT DISTINCT m.email_id FROM notification_events e "
            "JOIN notification_event_recipients r ON r.event_id = e.event_id AND r.is_active = 1 "
            "JOIN email_group_members m ON m.group_id = r.group_id AND m.is_active = 1 "
            "WHERE e.event_code = %s AND e.is_active = 1 ORDER BY m.email_id",
            (MSME_EVENT_CODE,),
        )
        emails = [row[0] for row in cursor.fetchall() if row[0]]
    except MySQLError as err:
        app.logger.warning("MSME email recipients lookup failed, using the maker: %s", err)
        emails = []
    finally:
        if cursor is not None:
            cursor.close()
        if conn is not None:
            conn.close()
    return emails or [MAKER_EMAIL]


def _send_msme_invoice_email(vendor, invoice_number, total_amount, currency, invoice_date):
    """Emails the business team that an invoice arrived from an MSME vendor. True when it was sent; False when the vendor
    is not an MSME or the send failed (the caller then falls back to the generic alert).

    The payment deadline is 45 days from the invoice date (today when the invoice carries none), the same rule the enterprise
    app's MSME tracker uses."""
    category = _msme_category(vendor.get("msme_status"))
    if category is None:
        return False
    due = (invoice_date or datetime.now(IST).date()) + timedelta(days=MSME_PAYMENT_DAYS)
    to, *cc = _msme_recipients()
    vendor_name = vendor.get("vendor_legal_name") or ""
    return _send_email(
        to,
        f"MSME invoice received: {vendor_name} {invoice_number}",
        MSME_INVOICE_TEMPLATE,
        {
            "vendor_name": vendor_name,
            "invoice_number": invoice_number,
            "invoice_amount": _format_amount(total_amount, currency) if total_amount is not None else "N/A",
            "msme_category": category,
            "udyam_number": str(vendor.get("udyam_number") or ""),
            "received_at": _now_ist_text(),
            "payment_due_date": due.strftime("%d %b %Y"),
            "review_url": ENTERPRISE_APP_URL,
        },
        cc=cc,
    )


def _send_invoice_emails(vendor_name, vendor_email, invoice_number, total_amount, currency, vendor=None, invoice_date=None):
    """The vendor's receipt, plus the alert to the business team. `vendor` is the vendor row (needs msme_status,
    udyam_number, vendor_legal_name): for an MSME vendor the MSME email replaces the generic "new invoice needs review"
    one, so the maker is not told twice about the same invoice."""
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
    if vendor is not None and _send_msme_invoice_email(vendor, invoice_number, total_amount, currency, invoice_date):
        return
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
    vendor["change_request"] = _change_request_of(vendor) if vendor["stage"] == auth.STAGE_CHANGE_REQUESTED else None
    vendor.pop("correction_fields", None)
    # The agreement waiting for the vendor's signature (onboarding or a renewal), if any.
    vendor["agreement"] = agreements.open_agreement_summary(vendor_id, vendor["stage"])
    # When the vendor's newest agreement was declined (the application is back with the checker), say so.
    vendor["declined_agreement"] = agreements.declined_agreement_summary(vendor_id)
    # On hold (no agreement in force) and the agreement's expiry, derived from the signed agreements' dates.
    try:
        vendor["agreement_hold"], vendor["agreement_expiry"] = agreement_hold.state_for(vendor_id, vendor.get("status"))
    except MySQLError:
        app.logger.exception("Could not read the agreement hold for vendor %s", vendor_id)
        vendor["agreement_hold"], vendor["agreement_expiry"] = None, None
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
@agreement_hold.write_guard
def save_my_draft():
    """Save the onboarding form as it stands (partial is fine). Only the values sent are written."""
    data = request.get_json(silent=True)
    if not isinstance(data, dict):
        return jsonify({"error": "Request body must be JSON"}), 400

    vendor_id = g.vendor["vendor_id"]
    values = _onboarding_values(data)
    # Drafts may be incomplete, so only the values actually provided are validated.
    provided = {field: value for field, value in values.items() if value is not None}
    validation_error = _validate_vendor_values(provided, draft=True)
    if validation_error:
        return jsonify({"error": validation_error}), 400
    # No duplicate-PAN check here: saving progress (and logging out) must always work, so the draft keeps exactly what was entered.
    # The PAN is checked again when the vendor continues past the first step and when the application is submitted.

    try:
        _save_onboarding(vendor_id, _load_vendor_row(vendor_id), values)
        return jsonify(_vendor_with_progress(vendor_id)), 200
    except MySQLError as err:
        return jsonify({"error": str(err)}), 500


@app.post("/vendors/me/submit")
@auth.vendor_required(auth.STAGE_DRAFT, auth.STAGE_RETURNED)
@agreement_hold.write_guard
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
        # The real guarantee behind the form's Verify buttons: PAN (and GSTIN / Aadhaar when given) must verify now.
        verification_error = verification.enforce_on_submit(final)
        if verification_error:
            return jsonify({"error": verification_error[0]}), verification_error[1]

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


@app.post("/vendors/verify/<service>")
@auth.vendor_required(auth.STAGE_DRAFT, auth.STAGE_RETURNED)
@agreement_hold.write_guard
def verify_identity(service):
    """Verify a PAN, GSTIN or Aadhaar number for the onboarding form (service is pan, gstin or aadhaar).

    Body: {"value": "...", "pan": "...", "gstin": "..."}; pan / gstin are the other tax ids in the form, used to
    check the PAN belongs to the GSTIN. The raw verification response is never returned.
    """
    service = service.upper()
    if service not in verification.SERVICES:
        return jsonify({"error": "Unknown verification service"}), 404
    data = request.get_json(silent=True)
    if not isinstance(data, dict):
        return jsonify({"error": "Request body must be JSON"}), 400
    body, status = verification.verify_for_form(service, data.get("value"), pan=data.get("pan"), gstin=data.get("gstin"))
    return jsonify(body), status


# --- Vendor-initiated "request changes" -----------------------------------------------------------------------
#
# While the application is with the maker or checker the vendor can ask to change some fields. Status becomes
# change_req_maker / change_req_checker (the value remembers whose queue it is in), vendor.correction_fields holds the
# field keys asked for, and that role is emailed. The reviewer then returns the application with the normal "Return for
# Correction" (status `returned`) or declines the request (the enterprise app restores Pending / 01). The vendor can
# also cancel while it is waiting. Resubmitting after a return follows the usual Pending -> maker -> checker path.

# The status that marks an open request, by the stage the application was in; and what cancelling restores.
_REQUEST_FROM_STAGE = {auth.STAGE_MAKER_REVIEW: auth.STATUS_CHANGE_REQ_MAKER, auth.STAGE_CHECKER_REVIEW: auth.STATUS_CHANGE_REQ_CHECKER}
_CANCEL_TO_STATUS = {auth.STATUS_CHANGE_REQ_MAKER: STATUS_PENDING, auth.STATUS_CHANGE_REQ_CHECKER: "01"}
_REQUEST_ROLE_GROUP = {auth.STATUS_CHANGE_REQ_MAKER: "MAKER", auth.STATUS_CHANGE_REQ_CHECKER: "CHECKER"}
MAX_REQUESTED_FIELDS = 30
# A dedicated template in the shared email service; until it is deployed the closest existing one is used.
CHANGE_REQUESTED_TEMPLATE = os.getenv("VENDOR_CHANGE_REQUESTED_TEMPLATE", "vendor_registration_submitted")


def _field_master(has_gstin):
    """{field_key: {field_key, field_label, section}} of what a vendor may ask to change (from rejection_reason).

    Fields the GSTIN lookup fills in are left out for a vendor who has a GSTIN, since they cannot edit those."""
    conn = cursor = None
    try:
        conn = get_connection()
        cursor = conn.cursor(dictionary=True)
        cursor.execute(
            "SELECT field_key, field_label, section, locked_by_gstin FROM rejection_reason "
            "WHERE is_active = 1 AND field_key IS NOT NULL ORDER BY sort_order, reason_id"
        )
        rows = cursor.fetchall()
    finally:
        if cursor is not None:
            cursor.close()
        if conn is not None:
            conn.close()
    fields = {}
    for row in rows:
        if has_gstin and row["locked_by_gstin"]:
            continue
        fields.setdefault(row["field_key"], {"field_key": row["field_key"], "field_label": row["field_label"], "section": row["section"]})
    return fields


def _change_request_of(vendor):
    """What the vendor asked for, for display: the fields (with labels) and whose queue it is waiting in."""
    keys = [key for key in str(vendor.get("correction_fields") or "").split(",") if key]
    master = _field_master(False)
    status = str(vendor.get("status") or "").strip().lower()
    return {
        "fields": [{"field_key": key, "field_label": master.get(key, {}).get("field_label") or key} for key in keys],
        "with_role": "checker" if status == auth.STATUS_CHANGE_REQ_CHECKER else "maker",
    }


def _group_emails(group_code, fallback):
    """The active members of an email group (Configure Emails screen in the enterprise app), else `fallback`."""
    conn = cursor = None
    try:
        conn = get_connection()
        cursor = conn.cursor()
        cursor.execute(
            "SELECT m.email_id FROM email_group_members m JOIN email_groups g ON g.group_id = m.group_id "
            "WHERE g.group_code = %s AND g.is_active = 1 AND m.is_active = 1",
            (group_code,),
        )
        emails = sorted({str(row[0]).strip() for row in cursor.fetchall() if row[0] and str(row[0]).strip()})
    except MySQLError:
        emails = []
    finally:
        if cursor is not None:
            cursor.close()
        if conn is not None:
            conn.close()
    return emails or [fallback]


def _send_change_request_email(vendor, new_status, labels):
    vendor_name = vendor.get("vendor_legal_name") or vendor.get("name") or vendor.get("email")
    request_id = f"VR-{vendor['vendor_id']}"
    subject = f"Vendor asked to change details - {vendor_name}"
    if CHANGE_REQUESTED_TEMPLATE == "vendor_change_requested":
        params = {
            "vendor_name": vendor_name,
            "vendor_email": vendor.get("email"),
            "fields": labels,
            "requested_at": _now_ist_text(),
            "request_id": request_id,
            "review_url": FRONTEND_BASE_URL,
        }
    else:
        # The "registration submitted" template has no field list, so the request goes in its "submitted by" line.
        params = {
            "vendor_name": vendor_name,
            "submitted_by": f"{vendor_name} ({vendor.get('email')}) asked to change: {', '.join(labels)}",
            "submitted_at": _now_ist_text(),
            "request_id": request_id,
            "review_url": FRONTEND_BASE_URL,
        }
    for recipient in _group_emails(_REQUEST_ROLE_GROUP[new_status], MAKER_EMAIL):
        _send_email(recipient, subject, CHANGE_REQUESTED_TEMPLATE, params)


@app.get("/vendors/me/change-request/fields")
@auth.vendor_required(auth.STAGE_MAKER_REVIEW, auth.STAGE_CHECKER_REVIEW, auth.STAGE_AGREEMENT)
def change_request_fields():
    """The form fields a vendor can ask to change."""
    conn = get_connection()
    try:
        cursor = conn.cursor(dictionary=True)
        cursor.execute("SELECT gstin FROM vendor WHERE vendor_id = %s", (g.vendor["vendor_id"],))
        row = cursor.fetchone() or {}
    finally:
        conn.close()
    fields = _field_master(bool(str(row.get("gstin") or "").strip()))
    return jsonify({"fields": list(fields.values())}), 200


@app.post("/vendors/me/change-request")
@auth.vendor_required(auth.STAGE_MAKER_REVIEW, auth.STAGE_CHECKER_REVIEW)
@agreement_hold.write_guard
def request_changes():
    """The vendor asks to change the ticked fields while their application is under review."""
    data = request.get_json(silent=True)
    requested = data.get("fields") if isinstance(data, dict) else None
    if not isinstance(requested, list) or not requested:
        return jsonify({"error": "Choose at least one field you want to change."}), 400
    keys = list(dict.fromkeys(str(key).strip() for key in requested if str(key).strip()))
    if not keys or len(keys) > MAX_REQUESTED_FIELDS:
        return jsonify({"error": f"Choose between 1 and {MAX_REQUESTED_FIELDS} fields."}), 400

    vendor_id = g.vendor["vendor_id"]
    new_status = _REQUEST_FROM_STAGE[g.vendor["stage"]]
    conn = cursor = None
    try:
        conn = get_connection()
        cursor = conn.cursor(dictionary=True)
        cursor.execute("SELECT gstin, vendor_legal_name, name, email FROM vendor WHERE vendor_id = %s", (vendor_id,))
        row = cursor.fetchone() or {}
        master = _field_master(bool(str(row.get("gstin") or "").strip()))
        unknown = [key for key in keys if key not in master]
        if unknown:
            return jsonify({"error": "One of the chosen fields cannot be changed this way.", "fields": unknown}), 400
        # Only while the status is still exactly what the stage was read from: a reviewer acting at the same moment wins.
        cursor.execute(
            "UPDATE vendor SET status = %s, correction_fields = %s WHERE vendor_id = %s AND status = %s",
            (new_status, ",".join(keys), vendor_id, g.vendor["status"]),
        )
        changed = cursor.rowcount
        conn.commit()
    except MySQLError as err:
        return jsonify({"error": str(err)}), 500
    finally:
        if cursor is not None:
            cursor.close()
        if conn is not None:
            conn.close()
    if not changed:
        return jsonify({"error": "Your application has just moved on, so the request was not sent. Please refresh."}), 409

    _send_change_request_email({**row, "vendor_id": vendor_id}, new_status, [master[key]["field_label"] for key in keys])
    return jsonify(_vendor_with_progress(vendor_id)), 200


@app.post("/vendors/me/change-request/cancel")
@auth.vendor_required(auth.STAGE_CHANGE_REQUESTED)
@agreement_hold.write_guard
def cancel_change_request():
    """The vendor withdraws the request: the application goes back to the queue it was in."""
    vendor_id = g.vendor["vendor_id"]
    restore = _CANCEL_TO_STATUS[str(g.vendor["status"]).strip().lower()]
    conn = cursor = None
    try:
        conn = get_connection()
        cursor = conn.cursor(dictionary=True)
        # A change request raised by "Something is wrong" at the agreement's details review goes back to that very step: the SAME
        # agreement awaits the vendor's signature again (in one transaction). Anything else is withdrawn as before.
        outcome = None
        if str(g.vendor["status"]).strip().lower() == auth.STATUS_CHANGE_REQ_CHECKER:
            outcome = agreements.restore_after_change_request_cancel(cursor, vendor_id, agreements._client_ip(), g.vendor.get("email"))
        if outcome == "restored":
            changed = 1
        elif outcome == "conflict":
            conn.rollback()
            changed = 0
        else:
            cursor.execute(
                "UPDATE vendor SET status = %s, correction_fields = NULL WHERE vendor_id = %s AND status = %s",
                (restore, vendor_id, g.vendor["status"]),
            )
            changed = cursor.rowcount
        conn.commit()
    except MySQLError as err:
        return jsonify({"error": str(err)}), 500
    finally:
        if cursor is not None:
            cursor.close()
        if conn is not None:
            conn.close()
    if not changed:
        return jsonify({"error": "Your request has just been handled by our team. Please refresh."}), 409
    return jsonify(_vendor_with_progress(vendor_id)), 200


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
@agreement_hold.write_guard
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
        # Uploading a document type replaces what was uploaded for it (a vendor fixing a returned application), unless the request
        # says ?append=1: a type that takes several files (Other) then adds to what is there, up to its limit.
        append = request.args.get("append") in ("1", "true")
        if append:
            for doc_type, (label, max_files) in DOCUMENT_TYPES.items():
                incoming = sum(1 for item in staged if item[0] == doc_type)
                present = sum(1 for blob in existing_blobs if blob.name.startswith(f"{prefix}{doc_type}_"))
                if incoming and present + incoming > max_files:
                    return jsonify({"error": f"{label}: attach at most {max_files} file(s)."}), 400
        replaced_prefixes = set() if append else {f"{prefix}{doc_type}_" for doc_type, *_ in staged}
        replaced_blobs = [blob.name for blob in existing_blobs if replaced_prefixes and blob.name.startswith(tuple(replaced_prefixes))]
        # Signed agreements share the folder but are not uploads (see agreements.AGREEMENT_DOC_TYPE): they do not count against the cap.
        agreement_prefix = f"{prefix}{agreements.AGREEMENT_DOC_TYPE}_"
        kept_blobs = [blob for blob in existing_blobs if not blob.name.startswith(agreement_prefix)]
        if len(kept_blobs) - len(replaced_blobs) + len(staged) > DOCUMENT_MAX_PER_VENDOR:
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


def _documents_on_file(container, vendor_id):
    """The vendor's supporting documents in the container (agreements are not listed here). `blob_name` identifies one file."""
    documents = []
    for blob in container.list_blobs(name_starts_with=f"vendor_{vendor_id}/", include=["metadata"]):
        metadata = blob.metadata or {}
        if metadata.get("doc_type") == agreements.AGREEMENT_DOC_TYPE:
            continue  # agreements are listed on the Agreement screen, not with the supporting documents
        documents.append({
            "type": metadata.get("doc_type"),
            "original_name": unquote(metadata.get("original_name") or blob.name.rsplit("/", 1)[-1]),
            "size": blob.size,
            "uploaded_at": metadata.get("uploaded_at"),
            "blob_name": blob.name,
        })
    return documents


@app.get("/vendors/me/documents")
@auth.vendor_required()
def list_my_documents():
    """The supporting documents already uploaded, so the form can show them and offer to replace or remove them."""
    vendor_id = g.vendor["vendor_id"]
    container = _docs_container()
    if container is None:
        return jsonify({"documents": [], "storage_configured": False}), 200
    try:
        documents = _documents_on_file(container, vendor_id)
    except AzureError:
        app.logger.exception("Listing documents for vendor %s failed", vendor_id)
        return jsonify({"error": "Unable to load your documents right now."}), 502
    return jsonify({"documents": documents, "storage_configured": True}), 200


@app.get("/vendors/me/documents/content")
@auth.vendor_required()
def my_document_content():
    """The bytes of one of the caller's own uploaded documents (?blob_name=..., the identifier from the listing), for the preview."""
    vendor_id = g.vendor["vendor_id"]
    blob_name = (request.args.get("blob_name") or "").strip()
    # Only a file directly inside the caller's own folder can be named; anything else (another vendor, a path trick) is not found.
    folder = f"vendor_{vendor_id}/"
    if not blob_name.startswith(folder) or "/" in blob_name[len(folder):] or ".." in blob_name:
        return jsonify({"error": "Document not found."}), 404
    container = _docs_container()
    if container is None:
        return jsonify({"error": "Document storage is not configured."}), 503
    try:
        match = next((item for item in _documents_on_file(container, vendor_id) if item["blob_name"] == blob_name), None)
        if match is None:  # also excludes the agreement files, which are not listed with the supporting documents
            return jsonify({"error": "Document not found."}), 404
        content = container.get_blob_client(blob_name).download_blob().readall()
    except AzureError:
        app.logger.exception("Reading a document for vendor %s failed", vendor_id)
        return jsonify({"error": "Unable to load the document right now."}), 502
    extension = Path(blob_name).suffix.lower()
    response = Response(content, mimetype=DOCUMENT_CONTENT_TYPES.get(extension, "application/octet-stream"))
    response.headers["Cache-Control"] = "private, no-store"
    response.headers["X-Content-Type-Options"] = "nosniff"
    return response


@app.delete("/vendors/me/documents")
@auth.vendor_required(auth.STAGE_DRAFT, auth.STAGE_RETURNED)
@agreement_hold.write_guard
def delete_my_document():
    """Remove one uploaded supporting document ({doc_type, blob_name}, in the body or the query string)."""
    data = request.get_json(silent=True)
    data = data if isinstance(data, dict) else {}
    doc_type = str(data.get("doc_type") or request.args.get("doc_type") or "").strip()
    blob_name = str(data.get("blob_name") or request.args.get("blob_name") or "").strip()
    if doc_type not in DOCUMENT_TYPES:
        return jsonify({"error": "Unknown document type."}), 400
    vendor_id = g.vendor["vendor_id"]
    # Only this vendor's own file of that type can be named: nothing else in the container is reachable from here.
    if not blob_name.startswith(f"vendor_{vendor_id}/{doc_type}_") or "/" in blob_name[len(f"vendor_{vendor_id}/"):]:
        return jsonify({"error": "Document not found."}), 404
    container = _docs_container()
    if container is None:
        return jsonify({"error": "Document storage is not configured."}), 503
    try:
        documents = _documents_on_file(container, vendor_id)
        if not any(item["blob_name"] == blob_name and item["type"] == doc_type for item in documents):
            return jsonify({"error": "Document not found."}), 404
        container.delete_blob(blob_name)
        documents = [item for item in documents if item["blob_name"] != blob_name]
    except AzureError:
        app.logger.exception("Removing a document for vendor %s failed", vendor_id)
        return jsonify({"error": "Unable to remove the document. Please try again."}), 502
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
@agreement_hold.write_guard
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
            f"SELECT vendor_id FROM vendor WHERE LOWER(TRIM(email)) = %s AND {EMAIL_HELD_SQL} LIMIT 1",
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

_PASSWORD_MIN_LENGTH = 8
_PASSWORD_MAX_LENGTH = 64
_PASSWORD_CHANGE_STAGES = (
    auth.STAGE_DRAFT, auth.STAGE_RETURNED, auth.STAGE_MAKER_REVIEW, auth.STAGE_CHECKER_REVIEW,
    auth.STAGE_AGREEMENT, auth.STAGE_AGREEMENT_SIGNED, auth.STAGE_APPROVED, auth.STAGE_CHANGE_REQUESTED,
    auth.STAGE_ACTIVE,
)


@app.post("/vendors/me/password")
@auth.vendor_required(*_PASSWORD_CHANGE_STAGES)
def change_vendor_password():
    """A signed-in vendor (any stage except rejected) chooses their own password, e.g. instead of the emailed one."""
    data = request.get_json(silent=True)
    if not isinstance(data, dict):
        return jsonify({"error": "Request body must be JSON"}), 400
    current = data.get("current_password")
    new = data.get("new_password")
    if not isinstance(current, str) or not current:
        return jsonify({"error": "current_password is required"}), 400
    if not isinstance(new, str) or not new:
        return jsonify({"error": "new_password is required"}), 400
    if len(new) < _PASSWORD_MIN_LENGTH or len(new) > _PASSWORD_MAX_LENGTH:
        return jsonify({"error": f"The new password must be {_PASSWORD_MIN_LENGTH} to {_PASSWORD_MAX_LENGTH} characters."}), 400
    if new == current:
        return jsonify({"error": "The new password must be different from the current one."}), 400

    vendor_id = g.vendor["vendor_id"]
    email = g.vendor["email"]
    # The same throttle as the login: a stolen session must not be able to guess the current password.
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
        cursor.execute("SELECT password FROM vendor WHERE vendor_id = %s", (vendor_id,))
        row = cursor.fetchone()
        stored_hash = row.get("password") if row else None
        submitted_hash = hashlib.sha256(current.encode("utf-8")).hexdigest()
        if not stored_hash or not hmac.compare_digest(str(stored_hash), submitted_hash):
            auth.record_login_failure(email)
            return jsonify({"error": "Current password is incorrect"}), 403

        auth.clear_login_failures(email)
        cursor.execute(
            "UPDATE vendor SET password = %s WHERE vendor_id = %s",
            (hashlib.sha256(new.encode("utf-8")).hexdigest(), vendor_id),
        )
        conn.commit()
        return jsonify({"message": "Your password has been changed."}), 200
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

    if not pan_duplicate_check_enabled():
        # TODO: re-enable after testing. While the switch is off no PAN is reported as already used.
        return jsonify({"message": "PAN not found", "code": 200, "exists": False, "data": []}), 200

    conn = None
    cursor = None
    try:
        conn = get_connection()
        cursor = conn.cursor(dictionary=True)

        # A vendor filling in their own form must not be told their own saved PAN is a duplicate.
        own_id = auth.current_vendor_id()
        cursor.execute(
            f"SELECT vendor_id FROM vendor WHERE UPPER(TRIM(pan)) = %s AND vendor_id <> %s AND {PAN_CLAIMED_SQL} LIMIT 1",
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


def _extract_invoice_from_ocr(invoice_file, file_bytes, vendor_id):
    """Send the file to the shared OCR service and turn its answer into database values.

    Shared by POST /invoices/ocr (insert) and POST /invoices/<id>/replace (overwrite).
    Returns (parsed, None) on success, or (None, (json_body, http_status)) when OCR fails so
    the caller can return that error unchanged. `parsed` holds: file_id, invoice_values (the
    invoices columns), tax_rows and line_rows (tuples WITHOUT the leading invoice_id, see
    _insert_invoice_children), invoice_number and ocr_data.
    """
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
        return None, ({"error": "Invoice OCR request failed", "details": details}, 502)
    except ValueError:
        return None, ({"error": "Invoice OCR service returned invalid JSON"}, 502)

    if not isinstance(ocr_data, dict):
        return None, ({"error": "Invoice OCR service returned an invalid response"}, 502)

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

    tax_rows = [
        (
            tax.get("tax_type") or tax.get("tax_desc"),
            tax.get("tax_description") or tax.get("tax_desc"),
            _decimal_value(tax.get("rate")),
            _decimal_value(tax.get("amount")),
        )
        for tax in invoice_data.get("tax_details") or []
    ]

    line_rows = []
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
        line_rows.append(
            (
                index,
                combined_item.get("product_code"),
                combined_item.get("description"),
                _date_value(combined_item.get("date")),
                _decimal_value(combined_item.get("quantity")),
                combined_item.get("unit"),
                _decimal_value(combined_item.get("unit_price")),
                _decimal_value(
                    combined_item.get("taxable_amount") or combined_item.get("amount")
                ),
                tax_amount,
                _decimal_value(
                    combined_item.get("total_amount") or combined_item.get("amount")
                ),
                extracted_tax_text,
                _json_value(combined_item),
            )
        )

    return {
        "file_id": file_id,
        "invoice_values": invoice_values,
        "tax_rows": tax_rows,
        "line_rows": line_rows,
        "invoice_number": invoice_number,
        "ocr_data": ocr_data,
    }, None


def _insert_invoice_children(cursor, invoice_id, tax_rows, line_rows):
    for tax_row in tax_rows:
        cursor.execute(
            "INSERT INTO invoice_tax_details "
            "(invoice_id, tax_type, tax_description, tax_rate, tax_amount) "
            "VALUES (%s, %s, %s, %s, %s)",
            (invoice_id, *tax_row),
        )
    for line_row in line_rows:
        cursor.execute(
            "INSERT INTO invoice_line_items "
            "(invoice_id, line_number, product_code, description, item_date, "
            "quantity, unit, unit_price, taxable_amount, tax_amount, "
            "total_amount, extracted_tax_text, raw_item_data) "
            "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)",
            (invoice_id, *line_row),
        )


@app.post("/invoices/ocr")
@auth.vendor_required(auth.STAGE_ACTIVE)
@agreement_hold.write_guard
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
            "SELECT vendor_id, email, vendor_legal_name, msme_status, udyam_number FROM vendor "
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

    parsed, ocr_error = _extract_invoice_from_ocr(invoice_file, file_bytes, vendor_id)
    if ocr_error is not None:
        return jsonify(ocr_error[0]), ocr_error[1]
    file_id = parsed["file_id"]
    invoice_values = parsed["invoice_values"]
    invoice_number = parsed["invoice_number"]
    ocr_data = parsed["ocr_data"]

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
        _insert_invoice_children(
            cursor, database_invoice_id, parsed["tax_rows"], parsed["line_rows"]
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
            vendor=vendor_row,
            invoice_date=invoice_values.get("invoice_date"),
        )

    return jsonify(
        {
            "message": "Invoice processed and saved successfully",
            "invoice_id": database_invoice_id,
            "file_id": file_id,
            "ocr_response": ocr_data,
        }
    ), 201


# MySQL errors meaning "the new return/replace tables or columns are not there yet".
_MISSING_SCHEMA_ERRNOS = (1146, 1054)  # ER_NO_SUCH_TABLE, ER_BAD_FIELD_ERROR


def _owned_invoice_id(cursor, invoice_id):
    """The invoice row id if this vendor uploaded it (invoices.vendor_id is the numeric id stored as text)."""
    cursor.execute(
        "SELECT id FROM invoices WHERE id = %s AND vendor_id = %s LIMIT 1",
        (invoice_id, str(g.vendor["vendor_id"])),
    )
    return cursor.fetchone()


def _vendor_return_payload(cursor, invoice_id):
    """Latest open return-to-vendor, else latest rejection, else None, shaped for the vendor.

    Internal wording (items with internal_only = 1) and who returned it never leave this function.
    """
    columns = "return_id, outcome, returned_at, overall_remark"
    cursor.execute(
        f"SELECT {columns} FROM invoice_return WHERE invoice_id = %s AND outcome = 'returned' "
        "AND returned_to = 'vendor' AND resolved_at IS NULL "
        "ORDER BY returned_at DESC, return_id DESC LIMIT 1",
        (invoice_id,),
    )
    header = cursor.fetchone()
    if header is None:
        cursor.execute(
            f"SELECT {columns} FROM invoice_return WHERE invoice_id = %s AND outcome = 'rejected' "
            "ORDER BY returned_at DESC, return_id DESC LIMIT 1",
            (invoice_id,),
        )
        header = cursor.fetchone()
    if header is None:
        return None

    cursor.execute(
        "SELECT item_id, field_key, field_label, reason_label, internal_only, remark, fixed_at "
        "FROM invoice_return_item WHERE return_id = %s ORDER BY item_id",
        (header["return_id"],),
    )
    all_items = cursor.fetchall()
    items = [
        {
            "item_id": row["item_id"],
            "field_key": row["field_key"],
            "field_label": row["field_label"],
            "reason_label": row["reason_label"],
            "remark": row["remark"],
            "fixed": row["fixed_at"] is not None,
        }
        for row in all_items
        if not row["internal_only"]
    ]
    if all_items and not items:
        items = [
            {
                "item_id": 0,
                "field_key": "file",
                "field_label": "Invoice",
                "reason_label": "Our team needs a corrected invoice",
                "remark": None,
                "fixed": False,
            }
        ]
    returned_at = header["returned_at"]
    return {
        "return_id": header["return_id"],
        "outcome": header["outcome"],
        "returned_at": returned_at.isoformat() if isinstance(returned_at, (datetime, date)) else returned_at,
        "overall_remark": header["overall_remark"],
        "items": items,
    }


@app.get("/invoices/<int:invoice_id>/return-reasons")
@auth.vendor_required(auth.STAGE_ACTIVE)
def get_invoice_return_reasons(invoice_id):
    conn = None
    cursor = None
    try:
        conn = get_connection()
        cursor = conn.cursor(dictionary=True)
        if _owned_invoice_id(cursor, invoice_id) is None:
            return jsonify({"error": "Invoice not found"}), 404
        try:
            payload = _vendor_return_payload(cursor, invoice_id)
        except MySQLError as err:
            # The invoice_return tables are created separately by the DBA; until then there is nothing to show.
            if getattr(err, "errno", None) not in _MISSING_SCHEMA_ERRNOS:
                raise
            payload = None
    except MySQLError as err:
        return jsonify({"error": str(err)}), 500
    finally:
        if cursor is not None:
            cursor.close()
        if conn is not None:
            conn.close()
    return jsonify(payload), 200


@app.post("/invoices/<int:invoice_id>/replace")
@auth.vendor_required(auth.STAGE_ACTIVE)
@agreement_hold.write_guard
def replace_invoice(invoice_id):
    invoice_file = request.files.get("invoices")
    if invoice_file is None or not invoice_file.filename:
        return jsonify({"error": "invoices file is required"}), 400
    file_bytes = invoice_file.read()
    if not file_bytes:
        return jsonify({"error": "invoices file must not be empty"}), 400

    vendor_id = str(g.vendor["vendor_id"])
    vendor_email = g.vendor.get("email")

    conn = None
    cursor = None
    try:
        conn = get_connection()
        cursor = conn.cursor(dictionary=True)
        if _owned_invoice_id(cursor, invoice_id) is None:
            return jsonify({"error": "Invoice not found"}), 404
        cursor.execute("SELECT status FROM invoices WHERE id = %s", (invoice_id,))
        current = cursor.fetchone() or {}
        if current.get("status") != "Returned to Vendor":
            return jsonify(
                {"error": "This invoice has not been returned to you, so it cannot be replaced."}
            ), 409
        cursor.execute(
            "SELECT vendor_legal_name, msme_status, udyam_number FROM vendor WHERE vendor_id = %s LIMIT 1", (vendor_id,)
        )
        vendor_row = cursor.fetchone() or {}
        vendor_name = vendor_row.get("vendor_legal_name")
    except MySQLError as err:
        return jsonify({"error": str(err)}), 500
    finally:
        if cursor is not None:
            cursor.close()
        if conn is not None:
            conn.close()

    parsed, ocr_error = _extract_invoice_from_ocr(invoice_file, file_bytes, vendor_id)
    if ocr_error is not None:
        return jsonify(ocr_error[0]), ocr_error[1]
    invoice_values = parsed["invoice_values"]

    # id, vendor_id, inv_ref_gen and created_at are never written; the row keeps its identity.
    updates = {k: v for k, v in invoice_values.items() if k not in ("vendor_id", "status")}
    set_clause = ", ".join(f"{column} = %s" for column in updates)

    conn = None
    cursor = None
    try:
        conn = get_connection()
        cursor = conn.cursor()
        # The invoice as it is now is kept as an earlier version first, in this same transaction: an invoice is never replaced
        # without its history. Nothing here (or anywhere) deletes the old file from storage.
        try:
            invoice_versions.snapshot_before_replace(cursor, invoice_id, vendor_email, "vendor")
        except Exception:  # noqa: BLE001 - any failure must stop the replace
            conn.rollback()
            app.logger.exception("Could not save the history of invoice %s before replacing it", invoice_id)
            return jsonify({"error": "Unable to save the invoice history, so the invoice was not replaced."}), 500
        # Back to the start of the approval chain (the maker's queue) whoever returned it, with the G/L account and tax code
        # cleared for the maker to assign again; it then goes to the checker and, if needed, the authorizer as usual.
        cursor.execute(
            f"UPDATE invoices SET {set_clause}, gl_account = NULL, tax_code = NULL, "
            "status = 'Pending Approval', replaced_at = NOW(), "
            "submission_round = submission_round + 1 "
            "WHERE id = %s AND vendor_id = %s AND status = 'Returned to Vendor'",
            [*updates.values(), invoice_id, vendor_id],
        )
        if cursor.rowcount == 0:
            # The invoice changed state while the OCR ran.
            conn.rollback()
            return jsonify(
                {"error": "This invoice has not been returned to you, so it cannot be replaced."}
            ), 409
        cursor.execute("DELETE FROM invoice_tax_details WHERE invoice_id = %s", (invoice_id,))
        cursor.execute("DELETE FROM invoice_line_items WHERE invoice_id = %s", (invoice_id,))
        _insert_invoice_children(cursor, invoice_id, parsed["tax_rows"], parsed["line_rows"])
        cursor.execute(
            "UPDATE invoice_return_item i JOIN invoice_return r ON r.return_id = i.return_id "
            "SET i.fixed_at = NOW() "
            "WHERE r.invoice_id = %s AND r.returned_to = 'vendor' AND r.resolved_at IS NULL "
            "AND i.fixed_at IS NULL",
            (invoice_id,),
        )
        cursor.execute(
            "UPDATE invoice_return SET resolved_at = NOW() "
            "WHERE invoice_id = %s AND returned_to = 'vendor' AND resolved_at IS NULL",
            (invoice_id,),
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

    if vendor_email:
        _send_invoice_emails(
            vendor_name,
            vendor_email,
            parsed["invoice_number"] or invoice_file.filename,
            invoice_values["total_amount"],
            invoice_values["currency"],
            vendor=vendor_row,
            invoice_date=invoice_values.get("invoice_date"),
        )

    return jsonify(
        {
            "message": "Invoice replaced and sent for review again",
            "invoice_id": invoice_id,
            "file_id": parsed["file_id"],
            "ocr_response": parsed["ocr_data"],
        }
    ), 200


# The columns of an invoice the details view shows. The preview of a new file returns just these, in the shape GET /invoices uses.
_PREVIEW_INVOICE_COLUMNS = (
    "invoice_number", "invoice_date", "due_date", "payment_term", "purchase_order", "currency",
    "subtotal", "taxable_total", "total_tax", "total_amount",
    "vendor_name", "extracted_vendor_name", "vendor_tax_id", "vendor_address",
    "customer_name", "customer_tax_id", "billing_address", "shipping_address",
    "vendor_gstin_verified", "customer_gstin_verified",
)
_PREVIEW_TAX_COLUMNS = ("tax_type", "tax_description", "tax_rate", "tax_amount")
_PREVIEW_LINE_COLUMNS = (
    "line_number", "product_code", "description", "item_date", "quantity", "unit", "unit_price",
    "taxable_amount", "tax_amount", "total_amount",
)


@app.post("/invoices/<int:invoice_id>/replace-preview")
@auth.vendor_required(auth.STAGE_ACTIVE)
@agreement_hold.write_guard
def preview_replacement_invoice(invoice_id):
    """Read a new file with the OCR service and return its details, so the vendor can compare them with the invoice
    being replaced before overwriting it. Nothing is saved: the invoice and its rows are untouched."""
    invoice_file = request.files.get("invoices")
    if invoice_file is None or not invoice_file.filename:
        return jsonify({"error": "invoices file is required"}), 400
    file_bytes = invoice_file.read()
    if not file_bytes:
        return jsonify({"error": "invoices file must not be empty"}), 400

    conn = None
    cursor = None
    try:
        conn = get_connection()
        cursor = conn.cursor(dictionary=True)
        if _owned_invoice_id(cursor, invoice_id) is None:
            return jsonify({"error": "Invoice not found"}), 404
        cursor.execute("SELECT status FROM invoices WHERE id = %s", (invoice_id,))
        if (cursor.fetchone() or {}).get("status") != "Returned to Vendor":
            return jsonify({"error": "This invoice has not been returned to you, so it cannot be replaced."}), 409
    except MySQLError as err:
        return jsonify({"error": str(err)}), 500
    finally:
        if cursor is not None:
            cursor.close()
        if conn is not None:
            conn.close()

    parsed, ocr_error = _extract_invoice_from_ocr(invoice_file, file_bytes, str(g.vendor["vendor_id"]))
    if ocr_error is not None:
        return jsonify(ocr_error[0]), ocr_error[1]

    invoice = _serialize_row({column: parsed["invoice_values"].get(column) for column in _PREVIEW_INVOICE_COLUMNS})
    invoice["tax_details"] = [_serialize_row(dict(zip(_PREVIEW_TAX_COLUMNS, row))) for row in parsed["tax_rows"]]
    invoice["line_items"] = [_serialize_row(dict(zip(_PREVIEW_LINE_COLUMNS, row))) for row in parsed["line_rows"]]
    return jsonify({"invoice": invoice}), 200


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
                "SELECT id FROM invoices WHERE vendor_id = %s "
                "OR (vendor_id = %s AND UPPER(TRIM(vendor_tax_id)) = %s)",
                (vendor_id, MAKER_VENDOR_ID, vendor_gstin),
            )
        else:
            cursor.execute("SELECT id FROM invoices WHERE vendor_id = %s", (vendor_id,))
        # Only the ids are filtered and sorted in SQL: sorting whole rows (they carry the large OCR
        # response) made MySQL fail with "Out of sort memory". The rows are fetched by primary key.
        matching_ids = sorted(row["id"] for row in cursor.fetchall())
        invoices = []
        for start in range(0, len(matching_ids), 200):
            chunk = matching_ids[start:start + 200]
            cursor.execute(
                f"SELECT * FROM invoices WHERE id IN ({', '.join(['%s'] * len(chunk))})",
                chunk,
            )
            invoices.extend(cursor.fetchall())
        invoices.sort(key=lambda invoice: invoice["id"])

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
            # 0 until the submission_round column exists (invoice table change for return/replace).
            serialized_invoice["submission_round"] = int(serialized_invoice.get("submission_round") or 0)
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


# Vendor agreement signing (vendor side). The shared helpers above are handed over so agreements.py needs no import of app.
agreements.configure(
    send_email=lambda *args, **kwargs: _send_email(*args, **kwargs),
    group_emails=lambda *args, **kwargs: _group_emails(*args, **kwargs),
    email_verification_request=lambda *args, **kwargs: _email_verification_request(*args, **kwargs),
    verified_email_error=lambda *args, **kwargs: _verified_email_error(*args, **kwargs),
    docs_container=lambda: _docs_container(),
    field_master=lambda *args, **kwargs: _field_master(*args, **kwargs),
    change_request_email=lambda *args, **kwargs: _send_change_request_email(*args, **kwargs),
    frontend_url=FRONTEND_BASE_URL,
    maker_email=MAKER_EMAIL,
    support_email=SUPPORT_EMAIL,
)
app.register_blueprint(agreements.bp)
app.register_blueprint(invoice_versions.bp)


if __name__ == "__main__":
    app.run(debug=True)
