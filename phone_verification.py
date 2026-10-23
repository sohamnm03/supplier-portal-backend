"""Phone number verification at signup: a 6-digit code sent over WhatsApp through the shared service.

Unlike email verification (where the shared service owns the code), the WhatsApp endpoint only delivers a message, so this
module owns the whole lifecycle: it makes the code, keeps a keyed hash of it in `phone_verification` (see
phone_verification.sql), sends it with the approved template, and counts attempts, expiry and resends.

The browser only ever holds the `verification_id`. Signup re-checks it on the server with `verified_phone_error`.
"""
import hashlib
import hmac
import logging
import os
import re
import secrets
import uuid
from datetime import datetime, timedelta, timezone

import requests
from flask import Blueprint, jsonify, request
from mysql.connector import Error as MySQLError

from db import get_connection

log = logging.getLogger(__name__)
bp = Blueprint("phone_verification", __name__)

CODE_LENGTH = 6
MAX_ATTEMPTS = 5
VALID_MINUTES = 10
RESEND_SECONDS = 60
MAX_SENDS_PER_HOUR = 5
SEND_TIMEOUT_SECONDS = 30

SHARED_SERVICES_URL = os.getenv("SHARED_SERVICES_URL", "https://fs-quad-shared.azurewebsites.net").rstrip("/")
WHATSAPP_MESSAGES_URL = f"{SHARED_SERVICES_URL}/api/whatsapp/messages"
# The approved Meta template: a body parameter with the code, plus a URL-button parameter (the one-tap "copy code").
OTP_TEMPLATE = os.getenv("WHATSAPP_OTP_TEMPLATE", "fsverifyphone")
OTP_TEMPLATE_LANGUAGE = os.getenv("WHATSAPP_OTP_TEMPLATE_LANGUAGE", "en_US")

PHONE_RE = re.compile(r"^\+[1-9][0-9]{7,14}$")
CODE_RE = re.compile(rf"^[0-9]{{{CODE_LENGTH}}}$")

# Without AUTH_SECRET_KEY the key is per process: a restart only invalidates codes that are at most VALID_MINUTES old.
_HASH_KEY = (os.getenv("AUTH_SECRET_KEY") or secrets.token_hex(32)).encode()

UNAVAILABLE = "Phone verification is unavailable right now. Please try again shortly."
NOT_VERIFIED = "Verify your phone number before continuing."
DEAD_MESSAGES = {
    "expired": ("This code has expired. Send a new one.", 410),
    "locked": ("Too many incorrect attempts. Send a new code.", 429),
    "superseded": ("A newer code was sent. Use the latest one, or send a new code.", 409),
}


class CodeNotSent(Exception):
    def __init__(self, message, status):
        super().__init__(message)
        self.message, self.status = message, status


def normalise_phone(value):
    return re.sub(r"[\s\-]", "", str(value or ""))


def _hash_code(verification_id, code):
    return hmac.new(_HASH_KEY, f"{verification_id}:{code}".encode(), hashlib.sha256).hexdigest()


def _reply(success, message=None, http_status=200, **extra):
    """`extra` goes into the body, including its own `status` field (pending / verified / expired ...)."""
    body = {"success": success, **extra}
    if message:
        body["message"] = message
    response = jsonify(body)
    response.status_code = http_status
    return response


def _wait_reply(message, seconds):
    seconds = max(1, int(seconds))
    response = _reply(False, message, 429, retry_after=seconds)
    response.headers["Retry-After"] = str(seconds)
    return response


def _wait_text(seconds):
    seconds = max(1, int(seconds))
    return f"{seconds} seconds" if seconds <= 90 else f"{-(-seconds // 60)} minutes"


def _status_of(row):
    if row["verified_at"] is not None:
        return "verified"
    if row["superseded_at"] is not None:
        return "superseded"
    if int(row["attempts"]) >= MAX_ATTEMPTS:
        return "locked"
    if not row["live"]:
        return "expired"
    return "pending"


def _load(cursor, verification_id):
    cursor.execute(
        "SELECT verification_id, phone, code_hash, attempts, verified_at, superseded_at, (expires_at > NOW()) AS live "
        "FROM phone_verification WHERE verification_id = %s",
        (verification_id,),
    )
    return cursor.fetchone()


def _send_whatsapp(phone, code, verification_id):
    """Hand the code to the shared WhatsApp service. `accepted` means Meta took it, not that it was delivered."""
    api_key = os.getenv("SHARED_SERVICE_API_KEY", "").strip()
    if not api_key:
        log.error("SHARED_SERVICE_API_KEY is not set; phone verification codes cannot be sent.")
        raise CodeNotSent(UNAVAILABLE, 503)

    payload = {
        "to": phone,
        "type": "template",
        "template": {
            "name": OTP_TEMPLATE,
            "language": OTP_TEMPLATE_LANGUAGE,
            "components": [
                {"type": "body", "parameters": [{"type": "text", "text": code}]},
                {"type": "button", "sub_type": "url", "index": "0", "parameters": [{"type": "text", "text": code}]},
            ],
        },
        "client_reference": f"dsa-otp-{verification_id}",
    }
    try:
        upstream = requests.post(WHATSAPP_MESSAGES_URL, json=payload, headers={"X-API-Key": api_key}, timeout=SEND_TIMEOUT_SECONDS)
    except requests.RequestException as err:
        # Never log the payload: it carries the phone number and the code.
        log.warning("WhatsApp send failed: %s", type(err).__name__)
        raise CodeNotSent(UNAVAILABLE, 503) from err

    try:
        body = upstream.json()
    except ValueError:
        body = None
    body = body if isinstance(body, dict) else {}
    if upstream.ok and body.get("status") == "accepted":
        return
    if upstream.status_code == 400:
        raise CodeNotSent("Enter a valid phone number.", 400)
    if upstream.status_code == 401:
        log.error("The shared service rejected SHARED_SERVICE_API_KEY (is this app registered in WHATSAPP_APPS?).")
        raise CodeNotSent(UNAVAILABLE, 503)
    log.warning("WhatsApp send was not accepted: HTTP %s, provider_error=%s", upstream.status_code, body.get("provider_error"))
    raise CodeNotSent("We couldn't send the code to WhatsApp. Please check the number and try again.", 502)


@bp.post("/vendors/phone-verification/send")
def send_code():
    data = request.get_json(silent=True)
    phone = normalise_phone(data.get("phone") if isinstance(data, dict) else None)
    if not PHONE_RE.match(phone):
        return _reply(False, "Enter a valid phone number with its country code.", 400)

    conn = cursor = None
    try:
        conn = get_connection()
        cursor = conn.cursor(dictionary=True)
        cursor.execute(
            "SELECT COUNT(*) AS sends, TIMESTAMPDIFF(SECOND, MAX(created_at), NOW()) AS since_last_s, "
            "TIMESTAMPDIFF(SECOND, MIN(created_at), NOW()) AS since_first_s "
            "FROM phone_verification WHERE phone = %s AND created_at > NOW() - INTERVAL 1 HOUR",
            (phone,),
        )
        recent = cursor.fetchone() or {}
        sends = int(recent.get("sends") or 0)
        if sends and int(recent["since_last_s"]) < RESEND_SECONDS:
            wait = RESEND_SECONDS - int(recent["since_last_s"])
            return _wait_reply(f"Please wait {_wait_text(wait)} before asking for another code.", wait)
        if sends >= MAX_SENDS_PER_HOUR:
            wait = 3600 - int(recent["since_first_s"])
            return _wait_reply(f"Too many codes were requested for this number. Try again in {_wait_text(wait)}.", wait)

        verification_id = str(uuid.uuid4())
        code = f"{secrets.randbelow(10 ** CODE_LENGTH):0{CODE_LENGTH}d}"
        try:
            _send_whatsapp(phone, code, verification_id)
        except CodeNotSent as err:
            return _reply(False, err.message, err.status)

        # The message is out: the older code stops working and the new one is recorded.
        cursor.execute(
            "UPDATE phone_verification SET superseded_at = NOW() WHERE phone = %s AND verified_at IS NULL AND superseded_at IS NULL",
            (phone,),
        )
        cursor.execute(
            "INSERT INTO phone_verification (verification_id, phone, code_hash, expires_at) "
            "VALUES (%s, %s, %s, DATE_ADD(NOW(), INTERVAL %s MINUTE))",
            (verification_id, phone, _hash_code(verification_id, code), VALID_MINUTES),
        )
        conn.commit()
    except MySQLError:
        log.exception("Phone verification send failed")
        return _reply(False, UNAVAILABLE, 503)
    finally:
        if cursor is not None:
            cursor.close()
        if conn is not None:
            conn.close()

    resend_at = datetime.now(timezone.utc) + timedelta(seconds=RESEND_SECONDS)
    return _reply(
        True, "We sent a code to your WhatsApp.",
        verification_id=verification_id,
        resend_available_at=resend_at.isoformat(),
        expires_in_seconds=VALID_MINUTES * 60,
    )


def _dead_reply(status):
    message, http_status = DEAD_MESSAGES[status]
    return _reply(False, message, http_status, status=status)


@bp.post("/vendors/phone-verification/verify-otp")
def verify_otp():
    data = request.get_json(silent=True)
    data = data if isinstance(data, dict) else {}
    verification_id, otp = data.get("verification_id"), data.get("otp")
    if not isinstance(verification_id, str) or not verification_id.strip() or not isinstance(otp, str):
        return _reply(False, "verification_id and otp are required", 400)
    verification_id, otp = verification_id.strip(), otp.strip()
    if not CODE_RE.match(otp):
        return _reply(False, f"Enter the {CODE_LENGTH}-digit code.", 400, status="pending")

    conn = cursor = None
    try:
        conn = get_connection()
        cursor = conn.cursor(dictionary=True)
        row = _load(cursor, verification_id)
        if row is None:
            return _reply(False, "This verification was not found. Send a new code.", 404, status="not_found")
        status = _status_of(row)
        if status == "verified":
            return _reply(True, "Phone number verified.", status="verified")
        if status != "pending":
            return _dead_reply(status)

        # Count the attempt first, conditionally, so guesses made in parallel cannot get past the limit.
        cursor.execute(
            "UPDATE phone_verification SET attempts = attempts + 1 WHERE verification_id = %s AND attempts < %s "
            "AND verified_at IS NULL AND superseded_at IS NULL AND expires_at > NOW()",
            (verification_id, MAX_ATTEMPTS),
        )
        counted = cursor.rowcount == 1
        conn.commit()
        if not counted:  # it changed under us (expired, locked, replaced): say what it is now
            row = _load(cursor, verification_id)
            status = _status_of(row) if row else "expired"
            return _reply(True, "Phone number verified.", status="verified") if status == "verified" else _dead_reply(status if status != "pending" else "expired")

        if hmac.compare_digest(_hash_code(verification_id, otp), str(row["code_hash"])):
            cursor.execute(
                "UPDATE phone_verification SET verified_at = NOW() WHERE verification_id = %s AND verified_at IS NULL AND superseded_at IS NULL",
                (verification_id,),
            )
            conn.commit()
            return _reply(True, "Phone number verified.", status="verified")

        remaining = MAX_ATTEMPTS - (int(row["attempts"]) + 1)
        if remaining <= 0:
            return _dead_reply("locked")
        return _reply(False, "That code is not correct.", 400, status="pending", attempts_remaining=remaining)
    except MySQLError:
        log.exception("Phone verification check failed")
        return _reply(False, UNAVAILABLE, 503)
    finally:
        if cursor is not None:
            cursor.close()
        if conn is not None:
            conn.close()


@bp.get("/vendors/phone-verification/<verification_id>/status")
def verification_status(verification_id):
    conn = cursor = None
    try:
        conn = get_connection()
        cursor = conn.cursor(dictionary=True)
        row = _load(cursor, verification_id)
    except MySQLError:
        log.exception("Phone verification status failed")
        return _reply(False, UNAVAILABLE, 503)
    finally:
        if cursor is not None:
            cursor.close()
        if conn is not None:
            conn.close()
    if row is None:
        return _reply(False, "This verification was not found.", 404, status="not_found")
    return _reply(True, status=_status_of(row))


def verified_phone_error(verification_id, phone):
    """Return (message, http_status) unless `verification_id` is a verified verification of exactly this phone number.

    The table is the source of truth, so signup checks it on the server whatever the browser claims.
    """
    if not isinstance(verification_id, str) or not verification_id.strip():
        return NOT_VERIFIED, 403
    conn = cursor = None
    try:
        conn = get_connection()
        cursor = conn.cursor(dictionary=True)
        row = _load(cursor, verification_id.strip())
    except MySQLError:
        log.exception("Phone verification lookup failed")
        return UNAVAILABLE, 503
    finally:
        if cursor is not None:
            cursor.close()
        if conn is not None:
            conn.close()
    if row is None or _status_of(row) != "verified" or row["phone"] != normalise_phone(phone):
        return NOT_VERIFIED, 403
    return None
