"""Invoice history: the old version of a returned invoice is kept when it is replaced (table invoice_version, append-only).

The live `invoices` row keeps its id and inv_ref_gen. Before a replace overwrites it, snapshot_before_replace() stores the whole
row with its tax rows and line items; the vendor sees the earlier versions inside the invoice's detail popup. The diff between
two versions is a pure function (diff_snapshots) - the enterprise app has an identical copy.
"""
import json
import logging
import re
from datetime import date, datetime
from decimal import Decimal, InvalidOperation

from flask import Blueprint, g, jsonify
from mysql.connector import Error as MySQLError

import auth
from db import get_connection

log = logging.getLogger(__name__)
bp = Blueprint("invoice_versions", __name__)

MISSING_SCHEMA_ERRNOS = (1146, 1054)  # ER_NO_SUCH_TABLE, ER_BAD_FIELD_ERROR
INVOICE_JSON_FIELDS = {
    "vendor_gstin_data", "customer_gstin_data", "matched_invoice_ids", "duplicate_reasons", "score_breakdown",
    "duplicate_check_data", "raw_response",
}
LINE_ITEM_JSON_FIELDS = {"raw_item_data"}
# Columns that hold numbers (a snapshot stores Decimals as text; the detail view turns them back into numbers like GET /invoices).
DECIMAL_KEYS = {
    "subtotal", "taxable_total", "total_tax", "total_amount", "tax_rate", "tax_amount", "quantity", "unit_price", "taxable_amount",
    "duplicate_score", "file_size_bytes", "amount_in_words", "discount_total", "round_off",
}
MAKER_VENDOR_ID = "maker"

# (key, label) compared between two versions.
COMPARED = (
    ("invoice_number", "Invoice number"), ("invoice_date", "Invoice date"), ("due_date", "Due date"), ("purchase_order", "Purchase order"),
    ("payment_term", "Payment term"), ("currency", "Currency"), ("subtotal", "Subtotal"), ("taxable_total", "Taxable total"),
    ("total_tax", "Total tax"), ("total_amount", "Total amount"), ("vendor_name", "Vendor name"), ("vendor_tax_id", "Vendor GSTIN"),
    ("customer_name", "Customer name"), ("customer_tax_id", "Customer GSTIN"), ("billing_address", "Billing address"),
    ("shipping_address", "Shipping address"), ("original_file_name", "File"),
)
_DAY = re.compile(r"^\d{4}-\d{2}-\d{2}")


# --- serialising ---------------------------------------------------------------------------------------------

def _dict_rows(cursor):
    """The rows of the last query as dicts, whatever kind of cursor it is."""
    rows = cursor.fetchall()
    if rows and not isinstance(rows[0], dict):
        names = [column[0] for column in cursor.description]
        rows = [dict(zip(names, row)) for row in rows]
    return rows


def snapshot_value(value, key, json_fields=()):
    """JSON-safe value for the snapshot: Decimal as text, dates as ISO, bytes skipped (returns the marker _SKIP), JSON columns kept."""
    if isinstance(value, (bytes, bytearray)):
        if key in json_fields:
            try:
                return json.loads(bytes(value))
            except (TypeError, ValueError):
                return _SKIP
        return _SKIP
    if key in json_fields and isinstance(value, str):
        try:
            return json.loads(value)
        except ValueError:
            return value
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    return value


_SKIP = object()


def snapshot_row(row, json_fields=()):
    out = {}
    for key, value in row.items():
        value = snapshot_value(value, key, json_fields)
        if value is not _SKIP:
            out[key] = value
    return out


def _read_snapshot(cursor, invoice_id, lock):
    cursor.execute(f"SELECT * FROM invoices WHERE id = %s{' FOR UPDATE' if lock else ''}", (invoice_id,))
    rows = _dict_rows(cursor)
    if not rows:
        raise LookupError(f"Invoice {invoice_id} not found")
    row = rows[0]
    cursor.execute("SELECT * FROM invoice_tax_details WHERE invoice_id = %s ORDER BY id", (invoice_id,))
    tax = [snapshot_row(item) for item in _dict_rows(cursor)]
    cursor.execute("SELECT * FROM invoice_line_items WHERE invoice_id = %s ORDER BY line_number", (invoice_id,))
    lines = [snapshot_row(item, LINE_ITEM_JSON_FIELDS) for item in _dict_rows(cursor)]
    return row, {"invoice": snapshot_row(row, INVOICE_JSON_FIELDS), "tax_details": tax, "line_items": lines}


def snapshot_before_replace(cursor, invoice_id, replaced_by, replaced_by_role):
    """Store the invoice as it is now as the next version. Call inside the replace transaction, before the overwrite; raises on any
    failure so the caller rolls back (an invoice is never replaced without its history). Returns the version number."""
    row, snapshot = _read_snapshot(cursor, invoice_id, lock=True)
    version_no = int(row.get("submission_round") or 0) + 1
    cursor.execute(
        "SELECT return_id FROM invoice_return WHERE invoice_id = %s AND returned_to = %s AND resolved_at IS NULL "
        "ORDER BY returned_at DESC, return_id DESC LIMIT 1",
        (invoice_id, replaced_by_role),
    )
    found = _dict_rows(cursor)
    return_id = found[0]["return_id"] if found else None
    cursor.execute(
        "INSERT INTO invoice_version (invoice_id, version_no, inv_ref_gen, vendor_id, invoice_number, currency, total_amount, file_id, "
        "original_file_name, blob_name, blob_url, status_at_replace, return_id, replaced_by, replaced_by_role, snapshot_json) "
        "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)",
        (
            invoice_id, version_no, row.get("inv_ref_gen"), None if row.get("vendor_id") is None else str(row.get("vendor_id")),
            row.get("invoice_number"), row.get("currency"), row.get("total_amount"), row.get("file_id"), row.get("original_file_name"),
            row.get("blob_name"), row.get("blob_url"), row.get("status"), return_id, replaced_by, replaced_by_role,
            json.dumps(snapshot, default=str),
        ),
    )
    return version_no


# --- the diff -------------------------------------------------------------------------------------------------

def _plain(value):
    """A value as the string used for comparing and showing: decimals normalised (1500.00 == 1500), dates by day, empty as ''."""
    if value is None:
        return ""
    if isinstance(value, bool):
        return "yes" if value else "no"
    if isinstance(value, (int, float, Decimal)):
        text = str(value)
    else:
        text = str(value).strip()
    if _DAY.match(text):
        return text[:10]
    try:
        number = Decimal(text)
    except (InvalidOperation, ValueError):
        return text
    if not number.is_finite():
        return text
    plain = format(number.normalize(), "f")
    return "0" if plain in ("-0", "") else plain


def diff_snapshots(old, new):
    """[{key, label, from, to}] for what differs between two snapshots ({invoice, tax_details, line_items}); equal values are skipped."""
    old_invoice, new_invoice = (old or {}).get("invoice") or {}, (new or {}).get("invoice") or {}
    changes = []
    for key, label in COMPARED:
        before, after = _plain(old_invoice.get(key)), _plain(new_invoice.get(key))
        if before != after:
            changes.append({"key": key, "label": label, "from": before, "to": after})
    for key, label in (("line_items", "Line items"), ("tax_details", "Tax rows")):
        before, after = len((old or {}).get(key) or []), len((new or {}).get(key) or [])
        if before != after:
            changes.append({"key": key, "label": label, "from": str(before), "to": str(after)})
    return changes


# --- reading ----------------------------------------------------------------------------------------------------

def _load_snapshot(value):
    if isinstance(value, (bytes, bytearray)):
        value = bytes(value).decode("utf-8")
    if isinstance(value, str):
        value = json.loads(value)
    return value if isinstance(value, dict) else {}


def _iso(value):
    return value.isoformat() if isinstance(value, (datetime, date)) else value


def _number(value):
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return value


def versions_payload(version_rows, live_snapshot, submission_round):
    """The response of GET /invoices/<id>/versions. `version_rows` are the stored versions (any order), `live_snapshot` the current invoice."""
    ordered = sorted(version_rows, key=lambda row: row["version_no"])
    snapshots = [_load_snapshot(row["snapshot_json"]) for row in ordered]
    items = []
    for index, row in enumerate(ordered):
        following = snapshots[index + 1] if index + 1 < len(ordered) else live_snapshot
        items.append({
            "version_no": row["version_no"],
            "replaced_at": _iso(row.get("replaced_at")),
            "replaced_by": row.get("replaced_by"),
            "replaced_by_role": row.get("replaced_by_role"),
            "status_at_replace": row.get("status_at_replace"),
            "invoice_number": row.get("invoice_number"),
            "total_amount": _number(row.get("total_amount")),
            "currency": row.get("currency"),
            "original_file_name": row.get("original_file_name"),
            "has_file": bool(row.get("blob_url") or row.get("blob_name")),
            "return_id": row.get("return_id"),
            "changes": diff_snapshots(snapshots[index], following),
        })
    items.reverse()
    rounds = int(submission_round or 0)
    return {"current_version": rounds + 1, "versions": items, "earlier_versions_not_kept": max(0, rounds - len(items))}


def _numbers_back(row, numeric_keys):
    """A snapshot row in the shape GET /invoices returns: numeric columns as numbers again."""
    out = {}
    for key, value in row.items():
        if key in numeric_keys and isinstance(value, str):
            try:
                out[key] = float(value)
                continue
            except ValueError:
                pass
        out[key] = value
    return out


def version_detail(snapshot, live_invoice, live_tax, live_lines):
    """One stored version shaped like an item of GET /invoices (uploaded_by_type, submission_round, tax_details, line_items)."""
    def numeric(live_rows):
        keys = set(DECIMAL_KEYS)
        for item in live_rows:
            keys.update(key for key, value in item.items() if isinstance(value, (Decimal, float)))
        return keys

    invoice = _numbers_back(snapshot.get("invoice") or {}, numeric([live_invoice] if live_invoice else []))
    invoice["uploaded_by_type"] = "maker" if str(invoice.get("vendor_id")) == MAKER_VENDOR_ID else "vendor"
    invoice["submission_round"] = int(invoice.get("submission_round") or 0)
    invoice["tax_details"] = [_numbers_back(item, numeric(live_tax)) for item in snapshot.get("tax_details") or []]
    invoice["line_items"] = [_numbers_back(item, numeric(live_lines)) for item in snapshot.get("line_items") or []]
    return invoice


# --- routes (the vendor's own invoices only) ----------------------------------------------------------------------

def _own_invoice(cursor, invoice_id):
    cursor.execute("SELECT id, submission_round FROM invoices WHERE id = %s AND vendor_id = %s LIMIT 1", (invoice_id, str(g.vendor["vendor_id"])))
    rows = _dict_rows(cursor)
    return rows[0] if rows else None


_VERSION_COLUMNS = (
    "version_no, replaced_at, replaced_by, replaced_by_role, status_at_replace, invoice_number, total_amount, currency, "
    "original_file_name, blob_name, blob_url, file_id, return_id, snapshot_json"
)


def _open(handler):
    """Runs handler(cursor) on a connection; a missing history table reads as 'no versions', other database errors as 500."""
    conn = cursor = None
    try:
        conn = get_connection()
        cursor = conn.cursor(dictionary=True)
        return handler(cursor)
    except MySQLError as err:
        if getattr(err, "errno", None) in MISSING_SCHEMA_ERRNOS:
            return None
        return jsonify({"error": str(err)}), 500
    finally:
        if cursor is not None:
            cursor.close()
        if conn is not None:
            conn.close()


@bp.get("/invoices/<int:invoice_id>/versions")
@auth.vendor_required(auth.STAGE_ACTIVE)
def list_versions(invoice_id):
    def run(cursor):
        invoice = _own_invoice(cursor, invoice_id)
        if invoice is None:
            return jsonify({"error": "Invoice not found"}), 404
        cursor.execute(f"SELECT {_VERSION_COLUMNS} FROM invoice_version WHERE invoice_id = %s ORDER BY version_no", (invoice_id,))
        rows = _dict_rows(cursor)
        _, live = _read_snapshot(cursor, invoice_id, lock=False)
        return jsonify(versions_payload(rows, live, invoice.get("submission_round"))), 200

    result = _open(run)
    if result is None:  # the history table is not there yet
        return jsonify({"current_version": 1, "versions": [], "earlier_versions_not_kept": 0}), 200
    return result


def _one_version(cursor, invoice_id, version_no):
    invoice = _own_invoice(cursor, invoice_id)
    if invoice is None:
        return None
    cursor.execute(f"SELECT {_VERSION_COLUMNS} FROM invoice_version WHERE invoice_id = %s AND version_no = %s", (invoice_id, version_no))
    rows = _dict_rows(cursor)
    return rows[0] if rows else None


@bp.get("/invoices/<int:invoice_id>/versions/<int:version_no>")
@auth.vendor_required(auth.STAGE_ACTIVE)
def get_version(invoice_id, version_no):
    def run(cursor):
        row = _one_version(cursor, invoice_id, version_no)
        if row is None:
            return jsonify({"error": "Version not found"}), 404
        live_row, _ = _read_snapshot(cursor, invoice_id, lock=False)
        cursor.execute("SELECT * FROM invoice_tax_details WHERE invoice_id = %s ORDER BY id", (invoice_id,))
        live_tax = _dict_rows(cursor)
        cursor.execute("SELECT * FROM invoice_line_items WHERE invoice_id = %s ORDER BY line_number", (invoice_id,))
        live_lines = _dict_rows(cursor)
        return jsonify(version_detail(_load_snapshot(row["snapshot_json"]), live_row, live_tax, live_lines)), 200

    return _open(run) or (jsonify({"error": "Version not found"}), 404)


@bp.get("/invoices/<int:invoice_id>/versions/<int:version_no>/file")
@auth.vendor_required(auth.STAGE_ACTIVE)
def get_version_file(invoice_id, version_no):
    """Where that version's ORIGINAL file is kept ({blob_url, blob_name, file_id, original_file_name}). The invoice files live in the
    shared document storage, which the portal opens through the preview service with this blob address (as for the current file)."""
    def run(cursor):
        row = _one_version(cursor, invoice_id, version_no)
        if row is None or not (row.get("blob_url") or row.get("blob_name")):
            return jsonify({"error": "File not found"}), 404
        return jsonify({
            "blob_url": row.get("blob_url") or row.get("blob_name"), "blob_name": row.get("blob_name"),
            "file_id": row.get("file_id"), "original_file_name": row.get("original_file_name"),
        }), 200

    return _open(run) or (jsonify({"error": "File not found"}), 404)
