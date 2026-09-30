"""Storage for vendor profile update requests (the `vendor_update_request` MySQL table).

An active vendor never overwrites their own `vendor` row from the profile screen: "Send request"
saves the old and new values of the changed fields here, and the maker and checker review it in
the maker/checker app. Only the checker's approval copies the new values onto the vendor.

Statuses (plain text, like vendor.status): update requested -> sent for approval -> approved,
or rejected at either review step.
"""
import json
from datetime import datetime

from db import get_connection

STATUS_REQUESTED = "update requested"
STATUS_SENT = "sent for approval"
STATUS_APPROVED = "approved"
STATUS_REJECTED = "rejected"
OPEN_STATUSES = (STATUS_REQUESTED, STATUS_SENT)

JSON_COLUMNS = ("old_values", "new_values", "changed_fields")


def _clean(row):
    for column in JSON_COLUMNS:
        value = row.get(column)
        if isinstance(value, (str, bytes)):
            row[column] = json.loads(value)
    for key, value in list(row.items()):
        if isinstance(value, datetime):
            row[key] = value.strftime("%Y-%m-%d %H:%M:%S")
    return row


def _query(sql, params=(), commit=False):
    conn = get_connection()
    try:
        cursor = conn.cursor(dictionary=True)
        cursor.execute(sql, params)
        rows = cursor.fetchall() if cursor.with_rows else []
        last_id = cursor.lastrowid
        if commit:
            conn.commit()
        cursor.close()
        return rows, last_id
    finally:
        conn.close()


def create_request(vendor_id, old_values, new_values, changed_fields, requested_by):
    _, request_id = _query(
        "INSERT INTO vendor_update_request (vendor_id, status, old_values, new_values, changed_fields, requested_by)"
        " VALUES (%s, %s, %s, %s, %s, %s)",
        (
            vendor_id,
            STATUS_REQUESTED,
            json.dumps(old_values, default=str),
            json.dumps(new_values, default=str),
            json.dumps(changed_fields),
            requested_by,
        ),
        commit=True,
    )
    return get_request(request_id)


def get_request(request_id):
    rows, _ = _query("SELECT * FROM vendor_update_request WHERE request_id = %s", (request_id,))
    return _clean(rows[0]) if rows else None


def list_requests(vendor_id):
    rows, _ = _query(
        "SELECT * FROM vendor_update_request WHERE vendor_id = %s ORDER BY requested_at DESC, request_id DESC",
        (vendor_id,),
    )
    return [_clean(row) for row in rows]


def get_open_request(vendor_id):
    rows, _ = _query(
        "SELECT * FROM vendor_update_request WHERE vendor_id = %s AND status IN (%s, %s)"
        " ORDER BY request_id DESC LIMIT 1",
        (vendor_id, *OPEN_STATUSES),
    )
    return _clean(rows[0]) if rows else None
