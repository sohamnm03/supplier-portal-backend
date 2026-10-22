"""The agreement hold: an active vendor with no agreement in force is on hold, and every write is refused until a new one starts.

No grace period and no stored status: the state is derived from the signed agreements' dates on every request.
The same rule lives in the enterprise app (agreement_hold.py) - keep the two identical.

  * AGREEMENT_EXPIRY_HOLD (default true; false = never hold).
  * Only vendors whose status is 'active' can be on hold, and only once an agreement has been signed.
  * A signed agreement is in force from its start date to its end date, both inclusive (today = the server date).
  * Nothing in force: reason 'expired' (nothing signed in the future; expired_on = the latest end) or 'between_agreements'
    (a signed one starts later; next_start).
  * A checker's release (agreement_event 'hold_released' {until, reason}; 'hold_release_ended' ends it - the latest of the two types
    across the vendor's agreements counts) lifts the hold until that date.
"""
import json
import logging
import os
from datetime import date
from functools import wraps

from flask import g, jsonify
from mysql.connector import Error as MySQLError

from db import get_connection

log = logging.getLogger(__name__)

EXPIRING_DAYS = 60
HOLD_MESSAGE = "Your agreement has expired. Actions are on hold until a new agreement is signed."


def enabled():
    return (os.getenv("AGREEMENT_EXPIRY_HOLD") or "true").strip().lower() not in ("0", "false", "no", "off")


def _day(value):
    try:
        return date.fromisoformat(str(value)[:10]) if value else None
    except ValueError:
        return None


def _detail(value):
    if isinstance(value, (bytes, bytearray)):
        value = value.decode("utf-8")
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except ValueError:
            return {}
    return value if isinstance(value, dict) else {}


def _periods(signed_rows):
    out = []
    for row in signed_rows:
        start, end = _day(row.get("start_iso")), _day(row.get("end_iso"))
        if start is not None and end is not None:
            out.append((start, end))
    return out


def evaluate(status, signed_rows, release, today, is_enabled=True):
    """The hold state: {on_hold, reason, expired_on, next_start, released_until}. `release` is the latest release event
    ({'event_type', 'detail'}) or None."""
    state = {"on_hold": False, "reason": None, "expired_on": None, "next_start": None, "released_until": None}
    periods = _periods(signed_rows)
    if not is_enabled or str(status or "").strip().lower() != "active" or not periods:
        return state
    if any(start <= today <= end for start, end in periods):
        return state
    future = [start for start, _ in periods if start > today]
    state["reason"] = "between_agreements" if future else "expired"
    state["expired_on"] = max(end for _, end in periods).isoformat()
    state["next_start"] = min(future).isoformat() if future else None
    until = _day(_detail(release.get("detail")).get("until")) if release and release.get("event_type") == "hold_released" else None
    if until is not None and until >= today:
        state["released_until"] = until.isoformat()
        return state
    state["on_hold"] = True
    return state


def expiry_of(signed_rows, today):
    """{end_date, days_left, expiring} of the agreement in force (the one that ends last when several are), else None."""
    in_force = [(start, end) for start, end in _periods(signed_rows) if start <= today <= end]
    if not in_force:
        return None
    end = max(end for _, end in in_force)
    left = (end - today).days
    return {"end_date": end.isoformat(), "days_left": left, "expiring": left <= EXPIRING_DAYS}


SIGNED_SQL = (
    "SELECT agreement_id, version, JSON_UNQUOTE(JSON_EXTRACT(terms_json, '$.start_iso')) AS start_iso, "
    "JSON_UNQUOTE(JSON_EXTRACT(terms_json, '$.end_iso')) AS end_iso FROM vendor_agreement "
    "WHERE vendor_id = %s AND status = 'signed' ORDER BY version DESC"
)
_RELEASE = (
    "SELECT e.event_type, e.detail FROM agreement_event e JOIN vendor_agreement a ON a.agreement_id = e.agreement_id "
    "WHERE a.vendor_id = %s AND e.event_type IN ('hold_released', 'hold_release_ended') ORDER BY e.event_id DESC LIMIT 1"
)
_TODAY = "SELECT NOW() AS db_now"


def read_state(cursor, vendor_id, status, today=None):
    """(hold, expiry) for one vendor from the database. `today` defaults to the server date."""
    if today is None:
        cursor.execute(_TODAY)
        today = cursor.fetchone()["db_now"].date()
    cursor.execute(SIGNED_SQL, (vendor_id,))
    signed = cursor.fetchall()
    release = None
    if enabled() and str(status or "").strip().lower() == "active" and signed:
        cursor.execute(_RELEASE, (vendor_id,))
        release = cursor.fetchone()
    return evaluate(status, signed, release, today, enabled()), expiry_of(signed, today)


def state_for(vendor_id, status):
    """read_state() on its own connection."""
    conn = cursor = None
    try:
        conn = get_connection()
        cursor = conn.cursor(dictionary=True)
        return read_state(cursor, vendor_id, status)
    finally:
        if cursor is not None:
            cursor.close()
        if conn is not None:
            conn.close()


def hold_response(hold):
    return jsonify({
        "error": HOLD_MESSAGE, "code": "agreement_expired", "expired_on": hold.get("expired_on"), "reason": hold.get("reason"),
    }), 423


def write_guard(view):
    """Refuse a vendor's write with HTTP 423 while the vendor is on hold. Put it under @auth.vendor_required."""
    @wraps(view)
    def wrapper(*args, **kwargs):
        vendor = getattr(g, "vendor", None)
        if vendor is not None:
            try:
                hold, _ = state_for(vendor["vendor_id"], vendor.get("status"))
            except MySQLError:
                log.exception("Could not read the agreement hold for vendor %s", vendor.get("vendor_id"))
                hold = None  # the route itself would fail on the same database
            if hold and hold["on_hold"]:
                return hold_response(hold)
        return view(*args, **kwargs)
    return wrapper
