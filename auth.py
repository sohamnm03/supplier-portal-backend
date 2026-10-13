"""Vendor session tokens, login throttling and the route guard for the vendor portal.

Until now the portal trusted whatever `vendor_id` the browser sent. A signed token ties every request to the
vendor who logged in, and the guard also enforces which stage of the lifecycle may use an endpoint:

  registered / returned -> may fill in and submit the onboarding form
  active                -> may use invoices and profile change requests
"""
import logging
import os
import secrets
import time
from functools import wraps

from flask import g, jsonify, request
from itsdangerous import BadSignature, SignatureExpired, URLSafeTimedSerializer
from mysql.connector import Error as MySQLError

from db import get_connection

log = logging.getLogger(__name__)

TOKEN_MAX_AGE_SECONDS = 12 * 60 * 60

_secret = os.getenv("AUTH_SECRET_KEY")
if not _secret:
    # Tokens still work, but every restart logs everyone out. Set AUTH_SECRET_KEY in .env to keep sessions.
    _secret = secrets.token_hex(32)
    log.warning("AUTH_SECRET_KEY is not set; using a temporary key. Vendor sessions end when the server restarts.")
_serializer = URLSafeTimedSerializer(_secret, salt="vendor-portal-session")

# Lifecycle stages a vendor can be in, derived from vendor.status (the single source of truth).
STAGE_DRAFT = "draft"                    # signed up, form not submitted
STAGE_MAKER_REVIEW = "maker_review"
STAGE_CHECKER_REVIEW = "checker_review"
STAGE_APPROVED = "approved"              # checker approved, SAP vendor being created
STAGE_ACTIVE = "active"
STAGE_RETURNED = "returned"              # sent back to the vendor with reasons
STAGE_REJECTED = "rejected"              # final rejection (legacy / maker-created vendors)


def stage_for(status, submission_round=0):
    """Map vendor.status to a lifecycle stage."""
    value = str(status or "").strip().lower()
    if value == "registered":
        return STAGE_DRAFT
    if value == "returned":
        return STAGE_RETURNED
    if value == "active":
        return STAGE_ACTIVE
    if value in ("02", "approved"):
        return STAGE_APPROVED
    if value in ("01", "sent for approval"):
        return STAGE_CHECKER_REVIEW
    if value in ("pending", ""):
        return STAGE_MAKER_REVIEW
    return STAGE_REJECTED  # Rejected, Pending Correction, Returned to Vendor, ...


def issue_token(vendor_id, email):
    return _serializer.dumps({"vid": int(vendor_id), "email": email})


def _token_payload():
    header = request.headers.get("Authorization", "")
    if not header.lower().startswith("bearer "):
        return None
    try:
        return _serializer.loads(header[7:].strip(), max_age=TOKEN_MAX_AGE_SECONDS)
    except (BadSignature, SignatureExpired):
        return None


def current_vendor_id():
    """The vendor id from a valid token, or None (for public endpoints that behave differently when signed in)."""
    payload = _token_payload()
    return payload["vid"] if payload else None


def _requested_vendor_id():
    """The vendor id the caller is asking about, wherever this endpoint takes it."""
    candidates = [
        (request.view_args or {}).get("vendor_id"),
        request.args.get("vendor_id") or request.args.get("vendor"),
        request.form.get("vendor_id"),
    ]
    body = request.get_json(silent=True)
    if isinstance(body, dict):
        candidates.append(body.get("vendor_id"))
    for value in candidates:
        if value not in (None, ""):
            return value
    return None


def vendor_required(*allowed_stages):
    """Require a valid vendor token (and, when given, one of `allowed_stages`).

    Sets `g.vendor` to the vendor's current {vendor_id, email, status, stage}; the status is read from the
    database on every call, so a vendor who is returned or approved is moved immediately.
    """
    def decorator(view):
        @wraps(view)
        def wrapper(*args, **kwargs):
            payload = _token_payload()
            if payload is None:
                return jsonify({"error": "Please sign in again.", "code": "unauthenticated"}), 401

            requested = _requested_vendor_id()
            if requested is not None and str(requested) != str(payload["vid"]):
                return jsonify({"error": "You can only access your own account.", "code": "forbidden"}), 403

            conn = cursor = None
            try:
                conn = get_connection()
                cursor = conn.cursor(dictionary=True)
                cursor.execute(
                    "SELECT vendor_id, email, status, submission_round FROM vendor WHERE vendor_id = %s",
                    (payload["vid"],),
                )
                vendor = cursor.fetchone()
            except MySQLError as err:
                return jsonify({"error": str(err)}), 500
            finally:
                if cursor is not None:
                    cursor.close()
                if conn is not None:
                    conn.close()

            if vendor is None:
                return jsonify({"error": "Please sign in again.", "code": "unauthenticated"}), 401

            vendor["stage"] = stage_for(vendor["status"], vendor.get("submission_round"))
            if allowed_stages and vendor["stage"] not in allowed_stages:
                return jsonify({
                    "error": "This isn't available for your account yet.",
                    "code": "stage_not_allowed",
                    "stage": vendor["stage"],
                }), 403
            g.vendor = vendor
            return view(*args, **kwargs)
        return wrapper
    return decorator


# --- login throttling -------------------------------------------------------------------------------------
# In-memory (per process): enough to stop online guessing of the short vendor passwords on a single server.
_MAX_FAILURES = 5
_WINDOW_SECONDS = 15 * 60
_failures = {}


def _throttle_key(email):
    return (str(email or "").strip().lower(), request.remote_addr)


def login_blocked_for(email):
    """Seconds the caller must still wait, or 0."""
    entry = _failures.get(_throttle_key(email))
    if not entry:
        return 0
    count, first = entry
    if time.time() - first > _WINDOW_SECONDS:
        _failures.pop(_throttle_key(email), None)
        return 0
    return int(_WINDOW_SECONDS - (time.time() - first)) if count >= _MAX_FAILURES else 0


def record_login_failure(email):
    key = _throttle_key(email)
    count, first = _failures.get(key, (0, time.time()))
    if time.time() - first > _WINDOW_SECONDS:
        count, first = 0, time.time()
    _failures[key] = (count + 1, first)


def clear_login_failures(email):
    _failures.pop(_throttle_key(email), None)
