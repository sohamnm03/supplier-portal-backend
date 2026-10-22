"""Vendor agreement signing, vendor side (see "Vendor agreement signing - backend contract.txt", section 7 SELF-SERVICE).

The checker issues the agreement from the enterprise app (tables vendor_agreement, agreement_otp, agreement_event,
agreement_clause are shared). Here the vendor lists their agreements, opens the PDF, asks for a one-time code, signs
or declines. The shared pieces that live in app.py (email service, email-verification relay, blob container) are
handed over once through `configure(...)`, so this module has no import cycle with app.py.
"""
import base64
import binascii
import hmac
import json
import logging
import os
import re
from datetime import date, datetime, timedelta, timezone
from urllib.parse import quote

from azure.core.exceptions import AzureError
from azure.storage.blob import ContentSettings
from flask import Blueprint, Response, g, jsonify, request
from mysql.connector import Error as MySQLError

import agreement_hold
import agreement_pdf
import auth
from db import get_connection

log = logging.getLogger(__name__)
bp = Blueprint("agreements", __name__)

IST = timezone(timedelta(hours=5, minutes=30))
OTP_MAX_ATTEMPTS = 5
OTP_RESEND_SECONDS = 60
OTP_VALID_MINUTES = 10
NAME_MIN, NAME_MAX = 2, 150
REASON_MIN, REASON_MAX = 3, 500
CHECKER_GROUP = "CHECKER"
SIGNATURE_METHODS = {"draw": "Drawn signature", "type": "Typed signature", "upload": "Uploaded signature image"}
# Blob metadata doc_type of an agreement filed in the vendor's documents folder (the enterprise app files the unsigned copy the same way).
AGREEMENT_DOC_TYPE = "agreement"
_DATA_URL = re.compile(r"^data:image/(?:png|jpeg|jpg);base64,([A-Za-z0-9+/=\s]+)$")

# Stages that may see the Agreement screen; the actions also include agreement_signed so a double click gets a clear 409.
VIEW_STAGES = (auth.STAGE_AGREEMENT, auth.STAGE_AGREEMENT_SIGNED, auth.STAGE_ACTIVE)
# The list and the PDF (the vendor's record of every agreement, declined ones included) are open in every stage an agreement can exist in,
# also while the application is back with the reviewers after a decline or a change request. Signing and declining stay on VIEW_STAGES.
HISTORY_STAGES = VIEW_STAGES + (auth.STAGE_CHECKER_REVIEW, auth.STAGE_MAKER_REVIEW, auth.STAGE_CHANGE_REQUESTED, auth.STAGE_RETURNED)

# Filled in by app.py: send_email, group_emails, email_verification_request, verified_email_error, docs_container,
# frontend_url, maker_email, support_email.
deps = {}


def _storage():
    """The blob container for agreement files (the vendor documents container), or None when Azure Blob Storage is not configured."""
    return deps["docs_container"]()


def configure(**kwargs):
    deps.update(kwargs)


# --- small helpers ------------------------------------------------------------------------------------------

def _err(message, status, **extra):
    return jsonify({"error": message, **extra}), status


def _client_ip():
    forwarded = request.headers.get("X-Forwarded-For", "")
    first = forwarded.split(",")[0].strip()
    return (first or request.remote_addr or "")[:64]


def _iso(value):
    return value.isoformat() if isinstance(value, (datetime, date)) else value


def _json(value):
    if isinstance(value, (bytes, bytearray)):
        value = value.decode("utf-8")
    if isinstance(value, str):
        return json.loads(value)
    return value


def _mask_email(email):
    local, _, domain = str(email or "").partition("@")
    return f"{local[:1]}***@{domain}" if domain else ""


def _ist_text(moment=None):
    return (moment or datetime.now(IST)).strftime("%d %b %Y, %I:%M %p IST")


def _portal_public_url():
    return (os.getenv("PORTAL_PUBLIC_URL") or deps.get("frontend_url") or "").rstrip("/")


def _event(cursor, agreement_id, event_type, actor, ip=None, detail=None):
    cursor.execute(
        "INSERT INTO agreement_event (agreement_id, event_type, actor, detail, ip) VALUES (%s, %s, %s, %s, %s)",
        (agreement_id, event_type, actor, json.dumps(detail) if detail else None, ip),
    )


class _Db:
    """with _Db() as (conn, cursor): ... closes both; the caller commits."""

    def __enter__(self):
        self.conn = get_connection()
        self.cursor = self.conn.cursor(dictionary=True)
        return self.conn, self.cursor

    def __exit__(self, *exc):
        try:
            self.cursor.close()
        finally:
            self.conn.close()
        return False


# --- reading ------------------------------------------------------------------------------------------------

_LIST_COLUMNS = (
    "agreement_id, agreement_no, version, status, issued_at, expires_at, first_viewed_at, signed_at, signer_name, declined_at, decline_reason, "
    "signed_pdf_path, JSON_UNQUOTE(JSON_EXTRACT(terms_json, '$.start_iso')) AS start_iso, "
    "JSON_UNQUOTE(JSON_EXTRACT(terms_json, '$.end_iso')) AS end_iso, "
    "JSON_EXTRACT(terms_json, '$.terms') AS key_terms_json, "
    "JSON_UNQUOTE(JSON_EXTRACT(terms_json, '$.company.name')) AS company_name, "
    "JSON_UNQUOTE(JSON_EXTRACT(terms_json, '$.company.signatory_name')) AS company_signatory_name, "
    "JSON_UNQUOTE(JSON_EXTRACT(terms_json, '$.company.signatory_designation')) AS company_signatory_designation"
)
_FULL_COLUMNS = (
    "agreement_id, agreement_no, vendor_id, version, status, terms_json, unsigned_pdf_path, unsigned_pdf_sha256, "
    "signed_pdf_path, signed_pdf_sha256, issued_at, expires_at, first_viewed_at, signed_at, NOW() AS db_now"
)


def _past(moment, now):
    return moment is not None and now is not None and moment < now


def _end_date(value):
    try:
        return date.fromisoformat(str(value)[:10])
    except (TypeError, ValueError):
        return None


_KEY_TERMS = ("term_months", "payment_days", "notice_days", "confidentiality_years")


def _key_terms(value):
    """The headline terms from the stored snapshot; keys that are missing are left out."""
    try:
        terms = _json(value)
    except (TypeError, ValueError):
        return {}
    if not isinstance(terms, dict):
        return {}
    return {key: terms[key] for key in _KEY_TERMS if terms.get(key) is not None}


def summarise(rows, now, can_act):
    """Vendor-facing summaries (rows newest first) with derived_state awaiting|scheduled|active|expired|replaced.

    For a signed row (today = the server date): scheduled when its start is still ahead; replaced when a newer signed
    version has already started; expired when its end has passed; otherwise active."""
    today = now.date()
    started_signed = [
        r["version"] for r in rows
        if r["status"] == "signed" and (_end_date(r.get("start_iso")) is None or _end_date(r.get("start_iso")) <= today)
    ]
    out = []
    for r in rows:
        link_expired = r["status"] == "sent" and _past(r.get("expires_at"), now)
        start = _end_date(r.get("start_iso"))
        if r["status"] == "sent":
            state = "awaiting"
        elif r["status"] == "declined":
            state = "declined"
        elif start is not None and start > today:
            state = "scheduled"
        elif any(version > r["version"] for version in started_signed):
            state = "replaced"
        else:
            end = _end_date(r.get("end_iso"))
            state = "expired" if end is not None and end < today else "active"
        out.append({
            "agreement_id": r["agreement_id"],
            "agreement_no": r["agreement_no"],
            "version": r["version"],
            "status": r["status"],
            "issued_at": _iso(r.get("issued_at")),
            "signed_at": _iso(r.get("signed_at")),
            "signer_name": r.get("signer_name"),
            "declined_at": _iso(r.get("declined_at")),
            "decline_reason": _text(r.get("decline_reason")) if r["status"] == "declined" else "",
            "decline_step": "",
            "start_iso": r.get("start_iso"),
            "end_iso": r.get("end_iso"),
            "expires_at": _iso(r.get("expires_at")),
            "first_viewed_at": _iso(r.get("first_viewed_at")),
            "derived_state": state,
            "is_current": state == "active",
            "can_sign": r["status"] == "sent" and not link_expired and can_act,
            "link_expired": link_expired,
            "has_signed_pdf": bool(r.get("signed_pdf_path")),
            "key_terms": _key_terms(r.get("key_terms_json")),
            "company_name": r.get("company_name"),
            "company_signatory_name": r.get("company_signatory_name"),
            "company_signatory_designation": r.get("company_signatory_designation"),
            "update_info": None,
        })
    return out


# (key, label, group) of the details snapshot stored in terms_json at issue time.
_SNAPSHOT_FIELDS = (
    ("legal_name", "Legal name", "Company"), ("vendor_type", "Vendor type", "Company"), ("email", "Email", "Company"),
    ("phone", "Phone", "Company"), ("registration_number", "Registration number", "Company"),
    ("msme_status", "MSME status", "Company"), ("pan", "PAN", "Tax"), ("gstin", "GSTIN", "Tax"),
    ("address", "Registered address", "Address"), ("bank_holder", "Account holder", "Bank"),
    ("bank_name", "Bank name", "Bank"), ("bank_branch", "Branch", "Bank"), ("bank_ifsc", "IFSC", "Bank"),
    ("bank_account_masked", "Account number", "Bank"),
)
# What older agreements (no snapshot) still have: terms_json['vendor'][source] -> snapshot key.
_LEGACY_VENDOR_FIELDS = {"legal_name": "legal_name", "type": "vendor_type", "address": "address", "pan": "pan",
                         "gstin": "gstin", "email": "email"}
_TERMS_FIELDS = (
    ("term_months", "Term (months)"), ("payment_days", "Payment within (days)"),
    ("notice_days", "Notice period (days)"), ("confidentiality_years", "Confidentiality (years)"),
)


def _safe_json(value):
    try:
        data = _json(value)
    except (TypeError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _text(value):
    return "" if value is None else str(value).strip()


def _decline_step(cursor, agreement_id):
    """The step a declined agreement was declined from ('review' | 'sign'), '' when unknown."""
    cursor.execute(
        "SELECT detail FROM agreement_event WHERE agreement_id = %s AND event_type = 'declined' ORDER BY event_id DESC LIMIT 1",
        (agreement_id,),
    )
    row = cursor.fetchone()
    detail = _safe_json(row["detail"]) if row else {}
    return _text(detail.get("at_step"))


def _snapshot_of(terms):
    snapshot = terms.get("details_snapshot")
    return snapshot if isinstance(snapshot, dict) else None


def _update_info(cursor, item, all_rows):
    """What changed since the previous version (any status), for a re-issued agreement (version 2 or later)."""
    mine = next((r for r in all_rows if r["version"] == item["version"]), None)
    lower = [r for r in all_rows if r["version"] < item["version"]]
    previous = max(lower, key=lambda r: r["version"], default=None)
    new_terms = _safe_json(mine["terms_json"]) if mine else {}
    old_terms = _safe_json(previous["terms_json"]) if previous else {}
    new_snap, old_snap = _snapshot_of(new_terms), _snapshot_of(old_terms)
    snapshot_missing = not (new_snap and old_snap)
    labels = {key: (label, group) for key, label, group in _SNAPSHOT_FIELDS}
    if snapshot_missing:
        # Only what both versions recorded: the vendor fields of the agreement itself.
        def legacy(terms):
            vendor = terms.get("vendor") or {}
            return {key: vendor.get(source) for source, key in _LEGACY_VENDOR_FIELDS.items()}
        new_values, old_values = legacy(new_terms), legacy(old_terms)
        keys = [key for key, _, _ in _SNAPSHOT_FIELDS if key in new_values]
    else:
        new_values, old_values = new_snap, old_snap
        keys = [key for key, _, _ in _SNAPSHOT_FIELDS]
    changes, changed_keys = [], []
    for key in keys:
        old, new = _text(old_values.get(key)), _text(new_values.get(key))
        if old != new:
            label, group = labels[key]
            changes.append({"key": key, "label": label, "group": group, "from": old, "to": new})
            changed_keys.append(key)
    details_changed = bool(changes)
    terms_changed = False
    for key, label in _TERMS_FIELDS:
        old = _text((old_terms.get("terms") or {}).get(key))
        new = _text((new_terms.get("terms") or {}).get(key))
        if old != new:
            changes.append({"key": key, "label": label, "group": "Terms", "from": old, "to": new})
            terms_changed = True
    declined = previous is not None and previous["status"] in ("declined", "withdrawn")
    return {
        "previous_version": previous["version"] if previous else None,
        "previous_decline_reason": _text(previous.get("decline_reason")) if declined else "",
        "previous_decline_step": _decline_step(cursor, previous["agreement_id"]) if previous and previous["status"] == "declined" else "",
        "issue_note": _text(new_terms.get("issue_note")),
        "history": [
            {"version": r["version"], "status": r["status"], "issued_at": _iso(r.get("issued_at")),
             "declined_at": _iso(r.get("declined_at")), "signed_at": _iso(r.get("signed_at")),
             "reason": _text(r.get("decline_reason")) if r["status"] == "declined" else ""}
            for r in sorted(lower, key=lambda r: r["version"], reverse=True)
        ],
        "details_changed": details_changed,
        "terms_changed": terms_changed,
        "snapshot_missing": snapshot_missing,
        "changed_keys": changed_keys,
        "changes": changes,
    }


def declined_agreement_summary(vendor_id):
    """For /vendors/me: the vendor's newest agreement when it was declined, else None."""
    try:
        with _Db() as (_, cursor):
            cursor.execute(
                "SELECT agreement_id, agreement_no, version, status, decline_reason, declined_at FROM vendor_agreement "
                "WHERE vendor_id = %s ORDER BY version DESC LIMIT 1",
                (vendor_id,),
            )
            row = cursor.fetchone()
            if not row or row["status"] != "declined":
                return None
            return {
                "agreement_no": row["agreement_no"], "version": row["version"], "reason": row.get("decline_reason") or "",
                "declined_at": _iso(row.get("declined_at")), "at_step": _decline_step(cursor, row["agreement_id"]),
            }
    except MySQLError:
        log.exception("Could not read the declined agreement for vendor %s", vendor_id)
    return None


def _vendor_agreements(cursor, vendor_id, stage):
    cursor.execute(
        f"SELECT {_LIST_COLUMNS} FROM vendor_agreement WHERE vendor_id = %s AND status IN ('sent', 'signed', 'declined') "
        "ORDER BY version DESC",
        (vendor_id,),
    )
    rows = cursor.fetchall()
    cursor.execute("SELECT NOW() AS db_now")
    now = cursor.fetchone()["db_now"]
    items = summarise(rows, now, stage in (auth.STAGE_AGREEMENT, auth.STAGE_ACTIVE))
    for item in items:
        if item["status"] == "declined":
            item["decline_step"] = _decline_step(cursor, item["agreement_id"])  # 'review' | 'sign' | ''
    reissued = [i for i in items if i["status"] == "sent" and i["version"] >= 2]
    if reissued:
        cursor.execute(
            "SELECT agreement_id, version, status, decline_reason, terms_json, issued_at, declined_at, signed_at FROM vendor_agreement "
            "WHERE vendor_id = %s ORDER BY version DESC",
            (vendor_id,),
        )
        all_rows = cursor.fetchall()
        for item in reissued:
            item["update_info"] = _update_info(cursor, item, all_rows)
    return items


def open_agreement_summary(vendor_id, stage):
    """The open ('sent') agreement for /vendors/me, or None (also when the agreement tables are not available)."""
    try:
        with _Db() as (_, cursor):
            for item in _vendor_agreements(cursor, vendor_id, stage):
                if item["status"] == "sent":
                    return item
    except MySQLError:
        log.exception("Could not read the open agreement for vendor %s", vendor_id)
    return None


def _load(cursor, vendor_id, agreement_id):
    cursor.execute(
        f"SELECT {_FULL_COLUMNS} FROM vendor_agreement WHERE agreement_id = %s AND vendor_id = %s",
        (agreement_id, vendor_id),
    )
    return cursor.fetchone()


def _state_error(row):
    """(message, status) when this agreement can no longer be signed or declined, else None."""
    status = row["status"]
    if status == "signed":
        return "This agreement has already been signed.", 409
    if status == "withdrawn":
        return "This agreement was withdrawn. Please ask for a new one.", 409
    if status == "declined":
        return "This agreement was declined.", 409
    if status != "sent":
        return "This agreement is not available for signing.", 409
    if _past(row.get("expires_at"), row.get("db_now")):
        return "The signing period for this agreement has ended. Please ask the checker to issue a new one.", 410
    return None


def _kind(vendor_status):
    """'onboarding' | 'renewal' | None, from vendor.status."""
    value = str(vendor_status or "").strip().lower()
    if value == auth.STATUS_AGREEMENT_SENT:
        return "onboarding"
    if value == "active":
        return "renewal"
    return None


def _latest_otp(cursor, agreement_id):
    cursor.execute(
        "SELECT otp_id, code_hash, sent_to, attempts, used_at, (expires_at > NOW()) AS live, "
        "TIMESTAMPDIFF(SECOND, created_at, NOW()) AS age_s "
        "FROM agreement_otp WHERE agreement_id = %s ORDER BY otp_id DESC LIMIT 1",
        (agreement_id,),
    )
    return cursor.fetchone()


# --- routes: list and PDF ---------------------------------------------------------------------------------

@bp.get("/vendors/me/agreements")
@auth.vendor_required(*HISTORY_STAGES)
def list_my_agreements():
    try:
        with _Db() as (_, cursor):
            items = _vendor_agreements(cursor, g.vendor["vendor_id"], g.vendor["stage"])
    except MySQLError as err:
        return _err(str(err), 500)
    return jsonify(items), 200


@bp.get("/vendors/me/agreements/<int:agreement_id>/pdf")
@auth.vendor_required(*HISTORY_STAGES)
def agreement_pdf_file(agreement_id):
    vendor = g.vendor
    download = request.args.get("download") in ("1", "true")
    try:
        with _Db() as (conn, cursor):
            row = _load(cursor, vendor["vendor_id"], agreement_id)
            if row is None or row["status"] not in ("sent", "signed", "declined"):  # a declined one is kept for the vendor's records
                return _err("Agreement not found.", 404)
            signed = row["status"] == "signed" and row.get("signed_pdf_path")
            path = row["signed_pdf_path"] if signed else row.get("unsigned_pdf_path")
            if not path:
                return _err("The agreement file is not available.", 404)
            container = _storage()
            if container is None:
                return _err("Document storage is not configured.", 503)
            try:
                data = container.get_blob_client(path).download_blob().readall()
            except AzureError:
                log.exception("Could not read agreement blob %s", path)
                return _err("Unable to load the agreement right now.", 502)

            ip = _client_ip()
            first = False
            if row.get("first_viewed_at") is None:
                cursor.execute(
                    "UPDATE vendor_agreement SET first_viewed_at = NOW() WHERE agreement_id = %s AND first_viewed_at IS NULL",
                    (agreement_id,),
                )
                first = cursor.rowcount == 1
            if first:
                _event(cursor, agreement_id, "viewed", vendor["email"], ip)
            if download:
                _event(cursor, agreement_id, "downloaded", vendor["email"], ip, {"signed": bool(signed)})
            conn.commit()
    except MySQLError as err:
        return _err(str(err), 500)

    name = f"{row['agreement_no']}{'_signed' if signed else ''}.pdf"
    response = Response(data, mimetype="application/pdf")
    response.headers["Content-Disposition"] = f'{"attachment" if download else "inline"}; filename="{name}"'
    response.headers["Cache-Control"] = "private, no-store"
    return response


# --- routes: one-time code --------------------------------------------------------------------------------

@bp.post("/vendors/me/agreements/<int:agreement_id>/send-code")
@auth.vendor_required(*VIEW_STAGES)
def send_code(agreement_id):
    vendor = g.vendor
    try:
        with _Db() as (conn, cursor):
            row = _load(cursor, vendor["vendor_id"], agreement_id)
            if row is None:
                return _err("Agreement not found.", 404)
            problem = _state_error(row)
            if problem:
                return _err(*problem)
            if _kind(vendor["status"]) is None:
                return _err("This agreement cannot be signed at the moment.", 409)
            last = _latest_otp(cursor, agreement_id)
            if last and last["age_s"] is not None and last["age_s"] < OTP_RESEND_SECONDS:
                wait = OTP_RESEND_SECONDS - int(last["age_s"])
                response = jsonify({"error": f"Please wait {wait} seconds before asking for another code.", "retry_after": wait})
                response.status_code = 429
                response.headers["Retry-After"] = str(wait)
                return response

            upstream = deps["email_verification_request"]("POST", "/send", json={"email": vendor["email"].strip()})
            if upstream is None:
                return _err("Email verification is unavailable right now. Please try again shortly.", 503)
            try:
                body = upstream.json()
            except ValueError:
                return _err("Email verification returned an unexpected response.", 502)
            if not upstream.ok:
                response = jsonify({"error": (body or {}).get("message") or "The code could not be sent.", "retry_after": None})
                response.status_code = upstream.status_code if upstream.status_code in (400, 429, 503) else 502
                if upstream.headers.get("Retry-After"):
                    response.headers["Retry-After"] = upstream.headers["Retry-After"]
                return response
            verification_id = body.get("verification_id") if isinstance(body, dict) else None
            if not isinstance(verification_id, str) or not verification_id.strip() or len(verification_id) > 64:
                return _err("Email verification returned an unexpected response.", 502)

            # agreement_otp.code_hash is CHAR(64): it holds the shared service's verification id (the code itself is
            # only ever held by the service, so nothing secret is stored here).
            cursor.execute(
                "INSERT INTO agreement_otp (agreement_id, code_hash, sent_to, expires_at) "
                "VALUES (%s, %s, %s, DATE_ADD(NOW(), INTERVAL %s MINUTE))",
                (agreement_id, verification_id.strip(), vendor["email"].strip(), OTP_VALID_MINUTES),
            )
            _event(cursor, agreement_id, "code_sent", vendor["email"], _client_ip())
            conn.commit()
    except MySQLError as err:
        return _err(str(err), 500)
    return jsonify({
        "success": True,
        "message": f"We sent a code to {_mask_email(vendor['email'])}.",
        "sent_to": _mask_email(vendor["email"]),
        "expires_in_seconds": OTP_VALID_MINUTES * 60,
        "resend_after_seconds": OTP_RESEND_SECONDS,
    }), 200


@bp.post("/vendors/me/agreements/<int:agreement_id>/verify-code")
@auth.vendor_required(*VIEW_STAGES)
def verify_code(agreement_id):
    vendor = g.vendor
    data = request.get_json(silent=True)
    otp = data.get("otp") if isinstance(data, dict) else None
    if not isinstance(otp, str) or not otp.strip() or len(otp.strip()) > 10:
        return _err("Enter the code we emailed you.", 400)
    try:
        with _Db() as (conn, cursor):
            row = _load(cursor, vendor["vendor_id"], agreement_id)
            if row is None:
                return _err("Agreement not found.", 404)
            problem = _state_error(row)
            if problem:
                return _err(*problem)
            last = _latest_otp(cursor, agreement_id)
            if not last or last["used_at"] is not None or not last["live"]:
                return _err("This code has expired. Please ask for a new one.", 400, code="otp_expired")
            # Count the attempt before asking the service, so a failure to record it never gives a free guess.
            cursor.execute(
                "UPDATE agreement_otp SET attempts = attempts + 1 WHERE otp_id = %s AND attempts < %s AND used_at IS NULL",
                (last["otp_id"], OTP_MAX_ATTEMPTS),
            )
            counted = cursor.rowcount
            conn.commit()
            if not counted:
                return _err("Too many wrong attempts. Please ask for a new code.", 429, code="otp_attempts_exceeded")
            attempts_left = max(0, OTP_MAX_ATTEMPTS - (int(last["attempts"]) + 1))

            upstream = deps["email_verification_request"](
                "POST", "/verify-otp", json={"verification_id": last["code_hash"], "otp": otp.strip()}
            )
            if upstream is None:
                return _err("Email verification is unavailable right now. Please try again shortly.", 503)
            try:
                body = upstream.json()
            except ValueError:
                return _err("Email verification returned an unexpected response.", 502)
            body = body if isinstance(body, dict) else {}
            if not upstream.ok or body.get("success") is False:
                status = upstream.status_code if upstream.status_code in (400, 410, 429, 503) else 400
                if upstream.ok:
                    status = 400
                return _err(body.get("message") or "That code is not correct.", status, attempts_left=attempts_left)
            _event(cursor, agreement_id, "code_verified", vendor["email"], _client_ip())
            conn.commit()
    except MySQLError as err:
        return _err(str(err), 500)
    return jsonify({"success": True, "verified": True, "message": "Code confirmed."}), 200


# --- routes: sign / decline -------------------------------------------------------------------------------

def _signature_bytes(value):
    if not isinstance(value, str):
        raise ValueError("Add your signature.")
    match = _DATA_URL.match(value.strip())
    if not match:
        raise ValueError("The signature must be a PNG or JPEG image.")
    try:
        raw = base64.b64decode(re.sub(r"\s+", "", match.group(1)), validate=True)
    except (binascii.Error, ValueError) as err:
        raise ValueError("The signature is not a valid image.") from err
    return agreement_pdf.normalise_signature(raw)


def _text_field(data, key, label, low, high):
    value = data.get(key)
    value = re.sub(r"\s+", " ", value).strip() if isinstance(value, str) else ""
    if not low <= len(value) <= high:
        raise ValueError(f"{label} must be between {low} and {high} characters.")
    return value


def _checker_emails():
    return deps["group_emails"](CHECKER_GROUP, deps.get("maker_email"))


def _notify(env_name, recipients, subject, params):
    """Send through the shared email service; skipped (INFO) while the template setting is empty. Never raises."""
    template = (os.getenv(env_name) or "").strip()
    if not template:
        log.info("Agreement email skipped: %s is not set (%s)", env_name, subject)
        return
    for recipient in recipients:
        try:
            deps["send_email"](recipient, subject, template, params)
        except Exception:  # an email problem must never fail the request
            log.exception("Agreement email '%s' to %s failed", template, recipient)


def _vendor_names(vendor_id):
    with _Db() as (_, cursor):
        cursor.execute("SELECT vendor_legal_name, name, email FROM vendor WHERE vendor_id = %s", (vendor_id,))
        row = cursor.fetchone() or {}
    return row.get("vendor_legal_name") or row.get("name") or row.get("email"), row.get("email")


@bp.post("/vendors/me/agreements/<int:agreement_id>/sign")
@auth.vendor_required(*VIEW_STAGES)
def sign_agreement(agreement_id):
    vendor = g.vendor
    vendor_id, email = vendor["vendor_id"], str(vendor["email"] or "").strip()
    data = request.get_json(silent=True)
    if not isinstance(data, dict):
        return _err("Request body must be JSON.", 400)
    kind = _kind(vendor["status"])

    try:
        with _Db() as (conn, cursor):
            row = _load(cursor, vendor_id, agreement_id)
            if row is None:
                return _err("Agreement not found.", 404)
            problem = _state_error(row)
            if problem:
                return _err(*problem)
            if kind is None:
                return _err("This agreement cannot be signed at the moment.", 409)

            # Nothing below trusts the browser except the signature, name, designation and method.
            if data.get("details_confirmed") is not True:
                return _err("Please confirm that your details are correct before signing.", 400)
            try:
                signer_name = _text_field(data, "signer_name", "Your name", NAME_MIN, NAME_MAX)
                signer_designation = _text_field(data, "signer_designation", "Your designation", NAME_MIN, NAME_MAX)
                method = data.get("method")
                if method not in SIGNATURE_METHODS:
                    raise ValueError("Choose how you signed: draw, type or upload.")
                signature_png = _signature_bytes(data.get("signature"))
            except ValueError as err:
                return _err(str(err), 400)

            otp = _latest_otp(cursor, agreement_id)
            if (not otp or otp["used_at"] is not None or not otp["live"]
                    or str(otp["sent_to"] or "").strip().lower() != email.lower()):
                return _err("Please confirm the one-time code we emailed you before signing.", 403, code="otp_required")
            verified_problem = deps["verified_email_error"](otp["code_hash"], email)
            if verified_problem:
                return _err("Please confirm the one-time code we emailed you before signing.", verified_problem[1], code="otp_required")

            # Rebuild the SIGNED pdf from the saved terms (never from anything the browser sent).
            try:
                terms = _json(row["terms_json"])
                company_signature = terms.pop("company_signature_b64", None)
                company_png = base64.b64decode(company_signature) if company_signature else None
                now_text = _ist_text()
                certificate = {
                    "signer_name": signer_name,
                    "signer_designation": signer_designation,
                    "method_label": SIGNATURE_METHODS[method],
                    "signed_at_text": now_text,
                    "ip": _client_ip(),
                    "user_agent": (request.headers.get("User-Agent") or "")[:400],
                    "otp_text": f"Yes, one-time code to {email} confirmed at {now_text}",
                    "confirmed_text": f"Yes, at {now_text}",
                }
                verify_url = (f"{_portal_public_url()}/verify-agreement?no={quote(row['agreement_no'])}"
                              f"&h={(row['unsigned_pdf_sha256'] or '')[:12]}")
                pdf = agreement_pdf.build_agreement_pdf(
                    terms, company_signature_png=company_png, vendor_signature_png=signature_png,
                    certificate=certificate, document_sha256=row["unsigned_pdf_sha256"], verify_url=verify_url,
                )
            except Exception:
                log.exception("Could not build the signed agreement %s", row["agreement_no"])
                return _err("Unable to prepare the signed agreement. Please try again.", 500)
            signed_sha = agreement_pdf.sha256_hex(pdf)

            container = _storage()
            if container is None:
                return _err("Document storage is not configured. Please try again later.", 503)
            # Filed with the vendor's other documents: vendor_<id>/agreement_<no>_signed.pdf, same blob metadata as a supporting document.
            blob_path = f"vendor_{vendor_id}/{AGREEMENT_DOC_TYPE}_{row['agreement_no']}_signed.pdf"
            try:
                container.upload_blob(blob_path, pdf, overwrite=True,
                                      content_settings=ContentSettings(content_type="application/pdf"),
                                      metadata={
                                          "vendor_id": str(vendor_id),
                                          "doc_type": AGREEMENT_DOC_TYPE,
                                          "original_name": quote(f"{row['agreement_no']}_signed.pdf", safe=""),
                                          "uploaded_at": _ist_text(),
                                      })
            except AzureError:
                log.exception("Could not upload %s", blob_path)
                return _err("Unable to save the signed agreement. Please try again.", 502)

            ip = _client_ip()
            cursor.execute(
                "UPDATE vendor_agreement SET status = 'signed', signed_at = NOW(), signer_name = %s, signer_designation = %s, "
                "signature_method = %s, signer_ip = %s, signer_user_agent = %s, details_confirmed_at = NOW(), "
                "otp_verified_at = NOW(), signed_pdf_path = %s, signed_pdf_sha256 = %s "
                "WHERE agreement_id = %s AND vendor_id = %s AND status = 'sent' AND (expires_at IS NULL OR expires_at >= NOW())",
                (signer_name, signer_designation, method, ip, (request.headers.get("User-Agent") or "")[:400],
                 blob_path, signed_sha, agreement_id, vendor_id),
            )
            if cursor.rowcount != 1:
                conn.rollback()
                latest = _load(cursor, vendor_id, agreement_id) or row
                if latest["status"] in ("withdrawn", "declined"):
                    _discard_blob(container, blob_path)  # never the signed copy of an agreement that was signed meanwhile
                return _err(*(_state_error(latest) or ("This agreement is no longer open for signing.", 409)))
            cursor.execute("UPDATE agreement_otp SET used_at = NOW() WHERE otp_id = %s AND used_at IS NULL", (otp["otp_id"],))
            if cursor.rowcount != 1:
                conn.rollback()
                return _err("That code was already used. Please refresh.", 409)
            if kind == "onboarding":
                cursor.execute(
                    "UPDATE vendor SET status = %s WHERE vendor_id = %s AND status = %s",
                    (auth.STATUS_AGREEMENT_SIGNED, vendor_id, auth.STATUS_AGREEMENT_SENT),
                )
                if cursor.rowcount != 1:
                    conn.rollback()
                    return _err("Your application has just moved on. Please refresh.", 409)
            _event(cursor, agreement_id, "details_confirmed", email, ip)
            _event(cursor, agreement_id, "signed", email, ip, {"method": method, "signed_pdf_sha256": signed_sha})
            conn.commit()

            signed_items = _vendor_agreements(
                cursor, vendor_id, auth.STAGE_AGREEMENT_SIGNED if kind == "onboarding" else auth.STAGE_ACTIVE)
    except MySQLError as err:
        return _err(str(err), 500)

    vendor_name, _ = _vendor_names(vendor_id)
    base = deps.get("frontend_url")
    _notify("VENDOR_AGREEMENT_SIGNED_TEMPLATE", [email], f"Agreement {row['agreement_no']} signed",
            {"vendor_name": vendor_name, "agreement_no": row["agreement_no"], "signed_at": now_text, "portal_url": base})
    _notify("VENDOR_AGREEMENT_SIGNED_NOTICE_TEMPLATE", _checker_emails(), f"Vendor signed agreement - {vendor_name}",
            {"vendor_name": vendor_name, "agreement_no": row["agreement_no"], "signed_at": now_text, "review_url": base})
    mine = next((item for item in signed_items if item["agreement_id"] == agreement_id), None)
    return jsonify({
        "message": "Agreement signed.",
        "agreement": mine,
        "stage": auth.STAGE_AGREEMENT_SIGNED if kind == "onboarding" else auth.STAGE_ACTIVE,
    }), 200


@bp.post("/vendors/me/agreements/<int:agreement_id>/preview")
@auth.vendor_required(*VIEW_STAGES)
def preview_signed_agreement(agreement_id):
    """The agreement as it will look once signed, built from what the vendor has filled in so far. Nothing is saved or recorded and nothing
    here is trusted for the real signing (sign rebuilds the PDF itself). `final: true` also appends the signing certificate page."""
    vendor = g.vendor
    data = request.get_json(silent=True)
    if not isinstance(data, dict):
        return _err("Request body must be JSON.", 400)
    name = re.sub(r"\s+", " ", data["signer_name"]).strip()[:NAME_MAX] if isinstance(data.get("signer_name"), str) else ""
    designation = re.sub(r"\s+", " ", data["signer_designation"]).strip()[:NAME_MAX] if isinstance(data.get("signer_designation"), str) else ""
    method = data.get("method") if data.get("method") in SIGNATURE_METHODS else None
    signature_png = None
    if data.get("signature"):
        try:
            signature_png = _signature_bytes(data["signature"])
        except ValueError as err:
            return _err(str(err), 400)
    final = data.get("final") is True
    try:
        with _Db() as (_, cursor):
            row = _load(cursor, vendor["vendor_id"], agreement_id)
        if row is None or row["status"] != "sent":
            return _err("Agreement not found.", 404)
        problem = _state_error(row)
        if problem:
            return _err(*problem)
        terms = _json(row["terms_json"])
        company_signature = terms.pop("company_signature_b64", None)
        company_png = base64.b64decode(company_signature) if company_signature else None
        now_text = _ist_text()
        certificate = None
        if final and signature_png and method:
            email = str(vendor["email"] or "").strip()
            certificate = {
                "signer_name": name,
                "signer_designation": designation,
                "method_label": SIGNATURE_METHODS[method],
                "signed_at_text": now_text,
                "ip": _client_ip(),
                "user_agent": (request.headers.get("User-Agent") or "")[:400],
                "otp_text": f"Yes, one-time code to {email} confirmed at {now_text}",
                "confirmed_text": f"Yes, at {now_text}",
            }
        verify_url = (f"{_portal_public_url()}/verify-agreement?no={quote(row['agreement_no'])}"
                      f"&h={(row['unsigned_pdf_sha256'] or '')[:12]}")
        pdf = agreement_pdf.build_agreement_pdf(
            terms, company_signature_png=company_png, vendor_signature_png=signature_png, certificate=certificate,
            document_sha256=row["unsigned_pdf_sha256"], verify_url=verify_url,
            preview_signer={"name": name, "designation": designation, "signed_line": f"Signed electronically on {now_text}"},
        )
    except MySQLError as err:
        return _err(str(err), 500)
    except Exception:
        log.exception("Could not build the agreement preview %s", agreement_id)
        return _err("Unable to prepare the preview. Please try again.", 500)
    response = Response(pdf, mimetype="application/pdf")
    response.headers["Content-Disposition"] = 'inline; filename="agreement-preview.pdf"'
    response.headers["Cache-Control"] = "private, no-store"
    return response


def _discard_blob(container, path):
    try:
        container.delete_blob(path)
    except Exception:
        log.warning("Could not remove %s after a refused signing", path)


@bp.post("/vendors/me/agreements/<int:agreement_id>/decline")
@auth.vendor_required(*VIEW_STAGES)
def decline_agreement(agreement_id):
    vendor = g.vendor
    vendor_id, email = vendor["vendor_id"], vendor["email"]
    data = request.get_json(silent=True)
    reason = data.get("reason") if isinstance(data, dict) else None
    reason = re.sub(r"[ \t]+", " ", reason).strip() if isinstance(reason, str) else ""
    at_step = data.get("at_step") if isinstance(data, dict) else None
    if at_step is None:
        at_step = "sign"
    if at_step not in ("review", "sign"):
        return _err("at_step must be 'review' or 'sign'.", 400)
    # At the details review the ticked details say what is wrong, so the note is optional there.
    if at_step == "review" and not reason and isinstance(data.get("fields"), list) and data["fields"]:
        reason = "Details need to be corrected."
    if not REASON_MIN <= len(reason) <= REASON_MAX:
        return _err(f"Tell us why in {REASON_MIN} to {REASON_MAX} characters.", 400)
    kind = _kind(vendor["status"])
    # "Something is wrong" at the details review (onboarding) is a change request: the vendor says which details to change, it
    # goes to the checker's Change requested tab and the checker returns the application for correction. Declining at the signing
    # step (or a renewal) keeps the plain "agreement declined" flow.
    change_request = at_step == "review" and kind == "onboarding"
    requested = data.get("fields") if isinstance(data, dict) else None
    keys = list(dict.fromkeys(str(key).strip() for key in requested if str(key).strip())) if isinstance(requested, list) else []
    if change_request and not keys:
        return _err("Choose at least one detail you want to change.", 400)
    if change_request and len(keys) > 30:
        return _err("Choose between 1 and 30 details.", 400)
    labels = []
    try:
        with _Db() as (conn, cursor):
            row = _load(cursor, vendor_id, agreement_id)
            if row is None:
                return _err("Agreement not found.", 404)
            problem = _state_error(row)
            if problem:
                return _err(*problem)
            if kind is None:
                return _err("This agreement cannot be declined at the moment.", 409)
            if change_request:
                cursor.execute("SELECT gstin FROM vendor WHERE vendor_id = %s", (vendor_id,))
                master = deps["field_master"](bool(str((cursor.fetchone() or {}).get("gstin") or "").strip()))
                unknown = [key for key in keys if key not in master]
                if unknown:
                    return _err("One of the chosen details cannot be changed this way.", 400, fields=unknown)
                labels = [master[key]["field_label"] for key in keys]
            cursor.execute(
                "UPDATE vendor_agreement SET status = 'declined', declined_at = NOW(), decline_reason = %s "
                "WHERE agreement_id = %s AND vendor_id = %s AND status = 'sent' AND (expires_at IS NULL OR expires_at >= NOW())",
                (reason, agreement_id, vendor_id),
            )
            if cursor.rowcount != 1:
                conn.rollback()
                latest = _load(cursor, vendor_id, agreement_id) or row
                return _err(*(_state_error(latest) or ("This agreement is no longer open.", 409)))
            if kind == "onboarding":
                if change_request:
                    cursor.execute(
                        "UPDATE vendor SET status = %s, correction_fields = %s WHERE vendor_id = %s AND status = %s",
                        (auth.STATUS_CHANGE_REQ_CHECKER, ",".join(keys), vendor_id, auth.STATUS_AGREEMENT_SENT),
                    )
                else:
                    cursor.execute(
                        "UPDATE vendor SET status = '01' WHERE vendor_id = %s AND status = %s",
                        (vendor_id, auth.STATUS_AGREEMENT_SENT),
                    )
                if cursor.rowcount != 1:
                    conn.rollback()
                    return _err("Your application has just moved on. Please refresh.", 409)
            _event(cursor, agreement_id, "declined", email, _client_ip(), {"reason": reason, "at_step": at_step, **({"fields": keys} if change_request else {})})
            conn.commit()
    except MySQLError as err:
        return _err(str(err), 500)

    vendor_name, _ = _vendor_names(vendor_id)
    if change_request:
        try:
            deps["change_request_email"]({"vendor_id": vendor_id, "vendor_legal_name": vendor_name, "email": email},
                                         auth.STATUS_CHANGE_REQ_CHECKER, labels)
        except Exception:  # an email problem must never fail the request
            log.exception("Change-request email for vendor %s failed", vendor_id)
        return jsonify({"message": "Your change request was sent.", "stage": auth.STAGE_CHANGE_REQUESTED}), 200
    _notify("VENDOR_AGREEMENT_DECLINED_TEMPLATE", _checker_emails(), f"Vendor declined agreement - {vendor_name}",
            {"vendor_name": vendor_name, "agreement_no": row["agreement_no"], "reason": reason,
             "review_url": deps.get("frontend_url")})
    return jsonify({
        "message": "You declined the agreement.",
        "stage": auth.STAGE_CHECKER_REVIEW if kind == "onboarding" else auth.STAGE_ACTIVE,
    }), 200


# --- public verification ----------------------------------------------------------------------------------

@bp.get("/agreements/verify")
def verify_agreement():
    """Public: is this agreement number + fingerprint a signed agreement? Nothing else about it is revealed."""
    number = (request.args.get("no") or "").strip()
    fingerprint = (request.args.get("h") or "").strip().lower()
    invalid = jsonify({"valid": False}), 200
    if not number or len(number) > 30 or not re.fullmatch(r"[0-9a-f]{12}", fingerprint):
        return invalid
    try:
        with _Db() as (_, cursor):
            cursor.execute(
                "SELECT agreement_no, status, unsigned_pdf_sha256, signed_at, "
                "JSON_UNQUOTE(JSON_EXTRACT(terms_json, '$.vendor.legal_name')) AS vendor_name "
                "FROM vendor_agreement WHERE agreement_no = %s",
                (number,),
            )
            row = cursor.fetchone()
    except MySQLError:
        log.exception("Agreement verification lookup failed")
        return invalid
    if (not row or row["status"] != "signed" or not row.get("unsigned_pdf_sha256")
            or not hmac.compare_digest(str(row["unsigned_pdf_sha256"])[:12].lower(), fingerprint)):
        return invalid
    return jsonify({
        "valid": True,
        "agreement_no": row["agreement_no"],
        "vendor_name": row["vendor_name"],
        "signed_at": _iso(row["signed_at"]),
    }), 200


# --- renewal request ---------------------------------------------------------------------------------------
# An active vendor whose agreement is ending (60 days or less) or has ended asks for a renewal. The request is recorded as
# agreement_event rows on the vendor's most recent signed agreement (the checkers act on it in the enterprise app):
#   renewal_requested {start_date, term_months, note, requested_by}   renewal_request_cancelled
#   renewal_request_declined {reason}   renewal_request_fulfilled {agreement_id}   (the last two are written by the enterprise app)
RENEWAL_OUTCOMES = {"renewal_request_declined": "declined", "renewal_request_fulfilled": "fulfilled", "renewal_request_cancelled": "cancelled"}
MAX_START_AHEAD_DAYS = 400
MIN_TERM_MONTHS, MAX_TERM_MONTHS = 1, 60
NOTE_MAX = 500
_RENEWAL_EVENTS_SQL = (
    "SELECT e.event_type, e.detail, e.created_at, e.actor FROM agreement_event e JOIN vendor_agreement a ON a.agreement_id = e.agreement_id "
    "WHERE a.vendor_id = %s AND e.event_type IN ('renewal_requested', 'renewal_request_cancelled', 'renewal_request_declined', "
    "'renewal_request_fulfilled') ORDER BY e.event_id DESC"
)


def _default_term_months():
    try:
        return int(os.getenv("AGREEMENT_DEFAULT_TERM_MONTHS") or 24)
    except ValueError:
        return 24


def renewal_window(signed_rows, today):
    """(earliest start, end of the agreement in force). The earliest start is the day after the agreement in force ends while that end is
    today or later, otherwise today. `signed_rows` are the signed agreements, newest version first, with start_iso / end_iso."""
    for row in signed_rows:
        start, end = _end_date(row.get("start_iso")), _end_date(row.get("end_iso"))
        if start is not None and start <= today:
            return (end + timedelta(days=1) if end is not None and end >= today else today), end
    return today, None


def _show(day):
    return f"{day.day} {day.strftime('%b %Y')}"


def resolve_renewal(start_date, term_months, signed_rows, today):
    """(start, term in months) of a renewal request; ValueError with a readable message when it is not allowed."""
    earliest, current_end = renewal_window(signed_rows, today)
    if term_months is None:
        term = _default_term_months()
    elif isinstance(term_months, bool) or not isinstance(term_months, int) or not MIN_TERM_MONTHS <= term_months <= MAX_TERM_MONTHS:
        raise ValueError(f"The term must be a whole number of months from {MIN_TERM_MONTHS} to {MAX_TERM_MONTHS}.")
    else:
        term = term_months
    if start_date in (None, ""):
        return earliest, term
    start = _end_date(start_date) if isinstance(start_date, str) else None
    if start is None:
        raise ValueError("The start date must be a date (YYYY-MM-DD).")
    if start < earliest:
        if current_end is not None and current_end >= today:
            raise ValueError(f"The new agreement can start only after the current agreement ends on {_show(current_end)} (earliest {_show(earliest)}).")
        raise ValueError("The start date cannot be in the past.")
    if start > earliest + timedelta(days=MAX_START_AHEAD_DAYS):
        raise ValueError(f"The start date can be at most {MAX_START_AHEAD_DAYS} days after {_show(earliest)}.")
    return start, term


def _renewal_state(cursor, vendor_id, status):
    """Everything the renewal endpoints need: the signed rows, today, eligibility, the open request and the last outcome."""
    cursor.execute("SELECT NOW() AS db_now")
    today = cursor.fetchone()["db_now"].date()
    cursor.execute(agreement_hold.SIGNED_SQL, (vendor_id,))
    signed = cursor.fetchall()
    hold, expiry = agreement_hold.read_state(cursor, vendor_id, status, today)
    eligible = bool(signed) and (hold["on_hold"] or bool(hold["released_until"]) or expiry is None or expiry["expiring"])
    cursor.execute(_RENEWAL_EVENTS_SQL, (vendor_id,))
    events = cursor.fetchall()
    open_request = last_outcome = None
    for index, event in enumerate(events):
        try:
            detail = _json(event.get("detail")) or {}
        except (TypeError, ValueError):
            detail = {}
        if not isinstance(detail, dict):
            detail = {}
        if event["event_type"] == "renewal_requested":
            if index == 0:
                open_request = {
                    "start_date": detail.get("start_date"), "term_months": detail.get("term_months"),
                    "note": detail.get("note") or "", "requested_at": _iso(event.get("created_at")),
                }
        elif last_outcome is None:
            last_outcome = {
                "status": RENEWAL_OUTCOMES[event["event_type"]], "reason": str(detail.get("reason") or ""), "at": _iso(event.get("created_at")),
            }
    return {"today": today, "signed": signed, "eligible": eligible, "open": open_request, "last": last_outcome}


def _renewal_payload(state):
    earliest, current_end = renewal_window(state["signed"], state["today"])
    return {
        "can_request": state["eligible"] and state["open"] is None,
        "min_start": earliest.isoformat(),
        "max_start": (earliest + timedelta(days=MAX_START_AHEAD_DAYS)).isoformat(),
        "default_term_months": _default_term_months(),
        "current_end": current_end.isoformat() if current_end else None,
        "open_request": state["open"],
        "last_outcome": state["last"],
    }


@bp.get("/vendors/me/renewal")
@auth.vendor_required(auth.STAGE_ACTIVE)
def get_renewal():
    try:
        with _Db() as (_, cursor):
            state = _renewal_state(cursor, g.vendor["vendor_id"], g.vendor["status"])
    except MySQLError as err:
        return _err(str(err), 500)
    return jsonify(_renewal_payload(state)), 200


@bp.post("/vendors/me/renewal-request")
@auth.vendor_required(auth.STAGE_ACTIVE)
def request_renewal():
    vendor = g.vendor
    data = request.get_json(silent=True)
    if not isinstance(data, dict):
        return _err("Request body must be JSON", 400)
    note = data.get("note")
    if note is not None and not isinstance(note, str):
        return _err("The note must be text.", 400)
    note = (note or "").strip()
    if len(note) > NOTE_MAX:
        return _err(f"The note can be at most {NOTE_MAX} characters.", 400)
    try:
        with _Db() as (conn, cursor):
            state = _renewal_state(cursor, vendor["vendor_id"], vendor["status"])
            if not state["signed"] or not state["eligible"]:
                return _err("A renewal can be requested when your agreement is ending soon or has ended.", 409)
            if state["open"] is not None:
                return _err("You already have a renewal request waiting for our team.", 409)
            try:
                start, term = resolve_renewal(data.get("start_date"), data.get("term_months"), state["signed"], state["today"])
            except ValueError as err:
                return _err(str(err), 400)
            _event(cursor, state["signed"][0]["agreement_id"], "renewal_requested", vendor["email"], _client_ip(),
                   {"start_date": start.isoformat(), "term_months": term, "note": note, "requested_by": vendor["email"]})
            conn.commit()
            state = _renewal_state(cursor, vendor["vendor_id"], vendor["status"])
    except MySQLError as err:
        return _err(str(err), 500)
    vendor_name, _ = _vendor_names(vendor["vendor_id"])
    _notify("AGREEMENT_RENEWAL_REQUESTED_TEMPLATE", _checker_emails(), f"Vendor requested an agreement renewal - {vendor_name}",
            {"vendor_name": vendor_name, "requested_start": start.isoformat(), "term_months": term, "note": note})
    return jsonify({"message": "We have told our team.", **_renewal_payload(state)}), 201


@bp.delete("/vendors/me/renewal-request")
@auth.vendor_required(auth.STAGE_ACTIVE)
def cancel_renewal_request():
    vendor = g.vendor
    try:
        with _Db() as (conn, cursor):
            state = _renewal_state(cursor, vendor["vendor_id"], vendor["status"])
            if state["open"] is None or not state["signed"]:
                return _err("There is no open renewal request to cancel.", 409)
            _event(cursor, state["signed"][0]["agreement_id"], "renewal_request_cancelled", vendor["email"], _client_ip(), None)
            conn.commit()
            state = _renewal_state(cursor, vendor["vendor_id"], vendor["status"])
    except MySQLError as err:
        return _err(str(err), 500)
    return jsonify({"message": "Your renewal request was cancelled.", **_renewal_payload(state)}), 200
