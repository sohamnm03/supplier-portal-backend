"""Reading and updating the reasons a maker/checker returned a self-service application for.

The maker/checker app writes `vendor_rejection` (one row per return) and `vendor_rejection_item` (one row per
flagged field). The portal only reads them and records the vendor's progress on them:

  fixed_at       - set when the vendor edits a flagged field (or confirms it is already correct)
  resubmitted_at - set on the return when the vendor resubmits

Flagged fields use the form's own keys (e.g. pan, ifsc_code) and doc_<type> for supporting documents.
"""
from datetime import datetime

from db import get_connection


def _iso(value):
    return value.isoformat() if isinstance(value, datetime) else value


def get_open_rejection(vendor_id):
    """The latest return the vendor has not resubmitted yet, with its flagged items, or None."""
    conn = get_connection()
    try:
        cursor = conn.cursor(dictionary=True)
        cursor.execute(
            "SELECT rejection_id, round_no, rejected_by_role, overall_remark, rejected_at "
            "FROM vendor_rejection WHERE vendor_id = %s AND resubmitted_at IS NULL "
            "ORDER BY rejected_at DESC, rejection_id DESC LIMIT 1",
            (vendor_id,),
        )
        rejection = cursor.fetchone()
        if rejection is None:
            return None
        cursor.execute(
            "SELECT item_id, reason_code, field_key, field_label, reason_label, remark, fixed_at "
            "FROM vendor_rejection_item WHERE rejection_id = %s ORDER BY item_id",
            (rejection["rejection_id"],),
        )
        rejection["items"] = [
            {**item, "fixed_at": _iso(item["fixed_at"]), "fixed": item["fixed_at"] is not None}
            for item in cursor.fetchall()
        ]
        rejection["rejected_at"] = _iso(rejection["rejected_at"])
        return rejection
    finally:
        conn.close()


def mark_fixed(vendor_id, field_keys):
    """Record that the vendor dealt with these flagged fields on their open return."""
    field_keys = [key for key in field_keys if key]
    if not field_keys:
        return
    conn = get_connection()
    try:
        cursor = conn.cursor()
        placeholders = ", ".join(["%s"] * len(field_keys))
        cursor.execute(
            "UPDATE vendor_rejection_item i JOIN vendor_rejection r ON r.rejection_id = i.rejection_id "
            "SET i.fixed_at = NOW() "
            f"WHERE r.vendor_id = %s AND r.resubmitted_at IS NULL AND i.fixed_at IS NULL "
            f"AND i.field_key IN ({placeholders})",
            [vendor_id, *field_keys],
        )
        conn.commit()
    finally:
        conn.close()


def mark_resubmitted(vendor_id):
    conn = get_connection()
    try:
        cursor = conn.cursor()
        cursor.execute(
            "UPDATE vendor_rejection SET resubmitted_at = NOW() WHERE vendor_id = %s AND resubmitted_at IS NULL",
            (vendor_id,),
        )
        conn.commit()
    finally:
        conn.close()
