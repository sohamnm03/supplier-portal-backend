"""Invoice history: the old version is kept when a returned invoice is replaced (invoice_versions.py)."""
import copy
import io
import json
import re
from datetime import datetime
from decimal import Decimal

import pytest
from mysql.connector import Error as MySQLError

import app as portal
import invoice_versions


# --- a small in-memory stand-in for the invoice tables ------------------------------------------------------

class InvoiceDb:
    def __init__(self):
        self.invoices = {}
        self.tax, self.lines, self.returns, self.versions = [], [], [], []
        self.fail_version_insert = False
        self.next_id = 100
        self._saved = None
        self.commit()

    def tables(self):
        return {"invoices": self.invoices, "tax": self.tax, "lines": self.lines, "returns": self.returns, "versions": self.versions}

    def commit(self):
        self._saved = copy.deepcopy(self.tables())

    def rollback(self):
        saved = copy.deepcopy(self._saved)
        self.invoices, self.tax, self.lines, self.returns, self.versions = (saved[k] for k in ("invoices", "tax", "lines", "returns", "versions"))

    def connect(self):
        return InvoiceConn(self)


class InvoiceConn:
    def __init__(self, db):
        self.db = db

    def cursor(self, dictionary=False):
        return InvoiceCursor(self.db, dictionary)

    def commit(self):
        self.db.commit()

    def rollback(self):
        self.db.rollback()

    def close(self):
        self.db.rollback()  # like MySQL: what was not committed is discarded


class InvoiceCursor:
    def __init__(self, db, dictionary):
        self.db, self.dictionary, self.rowcount, self._rows, self.description = db, dictionary, 0, [], None

    def close(self):
        pass

    def fetchone(self):
        rows = self.fetchall()
        return rows[0] if rows else None

    def fetchall(self):
        if self.dictionary or not self._rows:
            return list(self._rows)
        return [tuple(row.values()) for row in self._rows]

    def _set(self, rows):
        self._rows = [dict(row) for row in rows]
        self.description = [(key,) for key in self._rows[0]] if self._rows else None

    def execute(self, sql, params=()):
        db, s, p = self.db, re.sub(r"\s+", " ", sql).strip(), tuple(params)
        self.rowcount, self._rows = 0, []
        if s.startswith("SELECT id FROM invoices WHERE id = %s AND vendor_id = %s"):
            row = db.invoices.get(p[0])
            self._set([{"id": row["id"]}] if row and row["vendor_id"] == p[1] else [])
        elif s.startswith("SELECT id, submission_round FROM invoices WHERE id = %s AND vendor_id = %s"):
            row = db.invoices.get(p[0])
            self._set([{"id": row["id"], "submission_round": row["submission_round"]}] if row and row["vendor_id"] == p[1] else [])
        elif s == "SELECT status FROM invoices WHERE id = %s":
            self._set([{"status": db.invoices[p[0]]["status"]}])
        elif s.startswith("SELECT vendor_legal_name FROM vendor"):
            self._set([{"vendor_legal_name": "Vendor Pvt Ltd"}])
        elif s.startswith("SELECT * FROM invoices WHERE id = %s"):
            self._set([db.invoices[p[0]]] if p[0] in db.invoices else [])
        elif s.startswith("SELECT * FROM invoice_tax_details"):
            self._set([r for r in db.tax if r["invoice_id"] == p[0]])
        elif s.startswith("SELECT * FROM invoice_line_items"):
            self._set(sorted((r for r in db.lines if r["invoice_id"] == p[0]), key=lambda r: r["line_number"]))
        elif s.startswith("SELECT return_id FROM invoice_return WHERE invoice_id = %s AND returned_to = %s"):
            rows = [r for r in db.returns if r["invoice_id"] == p[0] and r["returned_to"] == p[1] and r["resolved_at"] is None]
            self._set([{"return_id": rows[-1]["return_id"]}] if rows else [])
        elif s.startswith("INSERT INTO invoice_version"):
            if db.fail_version_insert:
                raise MySQLError("history table is broken")
            cols = re.search(r"\(([^)]*)\) VALUES", s).group(1).split(", ")
            row = dict(zip(cols, p))
            if any(v["invoice_id"] == row["invoice_id"] and v["version_no"] == row["version_no"] for v in db.versions):
                raise MySQLError("duplicate version")
            row["replaced_at"] = datetime(2026, 10, 22, 15, 39)
            row["snapshot_json"] = row["snapshot_json"]
            db.versions.append(row)
        elif s.startswith("SELECT version_no, replaced_at") and "AND version_no = %s" in s:
            self._set([v for v in db.versions if v["invoice_id"] == p[0] and v["version_no"] == p[1]])
        elif s.startswith("SELECT version_no, replaced_at"):
            self._set(sorted((v for v in db.versions if v["invoice_id"] == p[0]), key=lambda v: v["version_no"]))
        elif s.startswith("UPDATE invoices SET"):
            match = re.match(r"UPDATE invoices SET (.*), gl_account = NULL, tax_code = NULL, status = 'Pending Approval', replaced_at = NOW\(\), "
                             r"submission_round = submission_round \+ 1 WHERE id = %s AND vendor_id = %s AND status = 'Returned to Vendor'$", s)
            assert match, s
            columns = [part.split(" = ")[0] for part in match.group(1).split(", ")]
            invoice_id, vendor_id = p[-2], p[-1]
            row = db.invoices.get(invoice_id)
            if row and row["vendor_id"] == vendor_id and row["status"] == "Returned to Vendor":
                row.update(dict(zip(columns, p[:len(columns)])))
                row.update(gl_account=None, tax_code=None, status="Pending Approval", submission_round=row["submission_round"] + 1)
                self.rowcount = 1
        elif s.startswith("DELETE FROM invoice_tax_details"):
            db.tax[:] = [r for r in db.tax if r["invoice_id"] != p[0]]
        elif s.startswith("DELETE FROM invoice_line_items"):
            db.lines[:] = [r for r in db.lines if r["invoice_id"] != p[0]]
        elif s.startswith("INSERT INTO invoice_tax_details"):
            db.tax.append({"id": len(db.tax) + 1000, "invoice_id": p[0], "tax_type": p[1], "tax_description": p[2], "tax_rate": p[3], "tax_amount": p[4]})
        elif s.startswith("INSERT INTO invoice_line_items"):
            db.lines.append({"id": len(db.lines) + 1000, "invoice_id": p[0], "line_number": p[1], "description": p[3], "total_amount": p[10]})
        elif s.startswith("UPDATE invoice_return_item"):
            pass
        elif s.startswith("UPDATE invoice_return SET resolved_at"):
            for r in db.returns:
                if r["invoice_id"] == p[0] and r["returned_to"] == "vendor" and r["resolved_at"] is None:
                    r["resolved_at"] = datetime(2026, 10, 22)
        else:
            raise AssertionError(f"unexpected SQL: {s}")


def invoice(invoice_id=5, vendor="7", round_=0, status="Returned to Vendor", **over):
    row = {
        "id": invoice_id, "vendor_id": vendor, "inv_ref_gen": f"INV-{invoice_id:05d}", "invoice_number": "A-1", "currency": "INR",
        "subtotal": Decimal("1000.00"), "total_tax": Decimal("180.00"), "total_amount": Decimal("1180.00"), "invoice_date": datetime(2026, 10, 1).date(),
        "file_id": "invoice-aaa", "original_file_name": "a.pdf", "blob_name": "blob/a.pdf", "blob_url": "https://x/blob/a.pdf",
        "status": status, "submission_round": round_, "vendor_name": "Vendor Pvt Ltd", "raw_response": json.dumps({"ok": True}),
        "ocr_bytes": b"\x00\x01", "created_at": datetime(2026, 10, 1, 10, 0),
    }
    row.update(over)
    return row


@pytest.fixture
def idb(env, monkeypatch):
    db = InvoiceDb()
    env.state.vendors[7]["status"] = "active"
    env.db.commit_now()
    db.invoices[5] = invoice()
    db.tax.append({"id": 1, "invoice_id": 5, "tax_type": "CGST", "tax_description": "CGST 9%", "tax_rate": Decimal("9.00"), "tax_amount": Decimal("90.00")})
    db.lines += [
        {"id": 1, "invoice_id": 5, "line_number": 1, "description": "Widget", "total_amount": Decimal("500.00"), "raw_item_data": json.dumps({"k": 1})},
        {"id": 2, "invoice_id": 5, "line_number": 2, "description": "Gadget", "total_amount": Decimal("500.00"), "raw_item_data": None},
    ]
    db.returns.append({"return_id": 31, "invoice_id": 5, "returned_to": "vendor", "returned_at": datetime(2026, 10, 20), "resolved_at": None})
    db.commit()
    for module in (portal, invoice_versions):
        monkeypatch.setattr(module, "get_connection", db.connect)
    env.idb = db
    return env


# --- the diff -----------------------------------------------------------------------------------------------

def snap(**invoice_fields):
    return {"invoice": invoice_fields, "tax_details": [], "line_items": []}


def test_diff_lists_only_what_changed_with_labels():
    old = snap(invoice_number="A-1", total_amount="1180.00", bank="x", currency="INR", billing_address=None)
    new = snap(invoice_number="A-2", total_amount="1180", bank="y", currency="INR", billing_address="")
    assert invoice_versions.diff_snapshots(old, new) == [{"key": "invoice_number", "label": "Invoice number", "from": "A-1", "to": "A-2"}]


def test_diff_normalises_decimals_and_dates():
    old = snap(total_amount="1500.00", invoice_date="2026-10-01T00:00:00", subtotal=1500.0)
    new = snap(total_amount=1500, invoice_date="2026-10-01", subtotal="1500.0000")
    assert invoice_versions.diff_snapshots(old, new) == []
    assert invoice_versions.diff_snapshots(snap(total_amount="1500.50"), snap(total_amount="1500.5")) == []
    change = invoice_versions.diff_snapshots(snap(total_amount="1180.00"), snap(total_amount="1250.50"))[0]
    assert (change["from"], change["to"], change["label"]) == ("1180", "1250.5", "Total amount")


def test_diff_counts_line_items_and_tax_rows_and_file():
    old = {"invoice": {"original_file_name": "a.pdf"}, "tax_details": [{}], "line_items": [{}, {}]}
    new = {"invoice": {"original_file_name": "b.pdf"}, "tax_details": [{}, {}, {}], "line_items": [{}]}
    assert invoice_versions.diff_snapshots(old, new) == [
        {"key": "original_file_name", "label": "File", "from": "a.pdf", "to": "b.pdf"},
        {"key": "line_items", "label": "Line items", "from": "2", "to": "1"},
        {"key": "tax_details", "label": "Tax rows", "from": "1", "to": "3"},
    ]


# --- the snapshot -------------------------------------------------------------------------------------------

def test_snapshot_copies_the_row_children_and_links_the_return(idb):
    db = idb.idb
    cursor = db.connect().cursor()  # a plain (tuple) cursor, like the replace route's
    assert invoice_versions.snapshot_before_replace(cursor, 5, "v@vendor.test", "vendor") == 1
    row = db.versions[0]
    assert (row["invoice_id"], row["version_no"], row["inv_ref_gen"], row["vendor_id"]) == (5, 1, "INV-00005", "7")
    assert (row["invoice_number"], row["currency"], row["total_amount"], row["file_id"]) == ("A-1", "INR", Decimal("1180.00"), "invoice-aaa")
    assert (row["original_file_name"], row["blob_name"], row["blob_url"]) == ("a.pdf", "blob/a.pdf", "https://x/blob/a.pdf")
    assert (row["status_at_replace"], row["return_id"], row["replaced_by"], row["replaced_by_role"]) == ("Returned to Vendor", 31, "v@vendor.test", "vendor")
    snapshot = json.loads(row["snapshot_json"])
    assert snapshot["invoice"]["total_amount"] == "1180.00" and snapshot["invoice"]["invoice_date"] == "2026-10-01"
    assert snapshot["invoice"]["raw_response"] == {"ok": True} and "ocr_bytes" not in snapshot["invoice"]
    assert [i["description"] for i in snapshot["line_items"]] == ["Widget", "Gadget"] and snapshot["line_items"][0]["raw_item_data"] == {"k": 1}
    assert snapshot["tax_details"][0]["tax_rate"] == "9.00"


def test_snapshot_without_an_open_return_has_no_return_id(idb):
    idb.idb.returns.clear()
    invoice_versions.snapshot_before_replace(idb.idb.connect().cursor(dictionary=True), 5, "m@x", "maker")
    assert idb.idb.versions[0]["return_id"] is None and idb.idb.versions[0]["replaced_by_role"] == "maker"


# --- the replace route -------------------------------------------------------------------------------------

def parsed_ok(number="A-2", total=Decimal("1250.50")):
    values = {"vendor_id": "7", "invoice_number": number, "currency": "INR", "total_amount": total, "file_id": "invoice-bbb",
              "original_file_name": "b.pdf", "blob_name": "blob/b.pdf", "blob_url": "https://x/blob/b.pdf", "status": "pending"}
    return {"invoice_values": values, "tax_rows": [("CGST", "CGST 9%", Decimal("9"), Decimal("100"))], "line_rows": [], "invoice_number": number,
            "file_id": "invoice-bbb", "ocr_data": {}}


def replace(env, monkeypatch, parsed=None):
    monkeypatch.setattr(portal, "_extract_invoice_from_ocr", lambda *a, **k: (parsed or parsed_ok(), None))
    monkeypatch.setattr(portal, "_send_invoice_emails", lambda *a, **k: None)
    return env.client.post("/invoices/5/replace", headers=env.headers, data={"invoices": (io.BytesIO(b"%PDF-1.4 x"), "b.pdf")},
                           content_type="multipart/form-data")


def test_replace_keeps_the_old_version_and_the_row_identity(idb, monkeypatch):
    r = replace(idb, monkeypatch)
    assert r.status_code == 200
    db = idb.idb
    live = db.invoices[5]
    assert live["id"] == 5 and live["inv_ref_gen"] == "INV-00005" and live["submission_round"] == 1 and live["invoice_number"] == "A-2"
    assert len(db.versions) == 1 and db.versions[0]["version_no"] == 1 and db.versions[0]["invoice_number"] == "A-1"
    assert db.versions[0]["blob_name"] == "blob/a.pdf"  # the old file is only referenced, never touched


def test_replace_is_refused_when_the_history_cannot_be_saved(idb, monkeypatch):
    idb.idb.fail_version_insert = True
    r = replace(idb, monkeypatch)
    assert r.status_code == 500 and r.get_json()["error"] == "Unable to save the invoice history, so the invoice was not replaced."
    live = idb.idb.invoices[5]
    assert live["invoice_number"] == "A-1" and live["status"] == "Returned to Vendor" and live["submission_round"] == 0
    assert idb.idb.versions == [] and len(idb.idb.lines) == 2


def test_version_numbers_over_two_replaces(idb, monkeypatch):
    assert replace(idb, monkeypatch).status_code == 200
    idb.idb.invoices[5]["status"] = "Returned to Vendor"
    idb.idb.commit()
    assert replace(idb, monkeypatch, parsed_ok("A-3", Decimal("1300"))).status_code == 200
    db = idb.idb
    assert [(v["version_no"], v["invoice_number"]) for v in db.versions] == [(1, "A-1"), (2, "A-2")]
    assert db.invoices[5]["submission_round"] == 2


# --- the read routes ---------------------------------------------------------------------------------------

def get(env, path):
    return env.client.get(path, headers=env.headers)


def two_versions(env, monkeypatch):
    replace(env, monkeypatch)
    env.idb.invoices[5]["status"] = "Returned to Vendor"
    env.idb.commit()
    replace(env, monkeypatch, parsed_ok("A-3", Decimal("1300")))


def test_versions_list_newest_first_with_changes(idb, monkeypatch):
    two_versions(idb, monkeypatch)
    body = get(idb, "/invoices/5/versions").get_json()
    assert body["current_version"] == 3 and body["earlier_versions_not_kept"] == 0
    assert [v["version_no"] for v in body["versions"]] == [2, 1]
    newest, oldest = body["versions"]
    assert newest["invoice_number"] == "A-2" and newest["total_amount"] == 1250.5 and newest["has_file"] is True
    assert {c["key"]: (c["from"], c["to"]) for c in newest["changes"]}["invoice_number"] == ("A-2", "A-3")
    assert {c["key"]: (c["from"], c["to"]) for c in oldest["changes"]}["total_amount"] == ("1180", "1250.5")
    assert oldest["return_id"] == 31 and oldest["replaced_by"] == "v@vendor.test" and oldest["replaced_by_role"] == "vendor"
    assert "snapshot_json" not in oldest and oldest["replaced_at"].startswith("2026-10-22")


def test_earlier_versions_not_kept(idb, monkeypatch):
    idb.idb.invoices[5]["submission_round"] = 3  # replaced three times before the history existed
    idb.idb.commit()
    replace(idb, monkeypatch)
    body = get(idb, "/invoices/5/versions").get_json()
    assert body["current_version"] == 5 and len(body["versions"]) == 1 and body["earlier_versions_not_kept"] == 3


def test_version_detail_has_the_shape_of_the_invoice_list(idb, monkeypatch):
    replace(idb, monkeypatch)
    body = get(idb, "/invoices/5/versions/1").get_json()
    assert body["invoice_number"] == "A-1" and body["total_amount"] == 1180.0 and body["invoice_date"] == "2026-10-01"
    assert body["uploaded_by_type"] == "vendor" and body["submission_round"] == 0 and body["status"] == "Returned to Vendor"
    assert [i["description"] for i in body["line_items"]] == ["Widget", "Gadget"] and body["line_items"][0]["total_amount"] == 500.0
    assert body["tax_details"][0]["tax_rate"] == 9.0 and body["raw_response"] == {"ok": True}
    assert get(idb, "/invoices/5/versions/9").status_code == 404


def test_version_file_is_owner_only(idb, monkeypatch):
    replace(idb, monkeypatch)
    r = get(idb, "/invoices/5/versions/1/file")
    assert r.status_code == 200 and r.get_json()["blob_url"] == "https://x/blob/a.pdf" and r.get_json()["original_file_name"] == "a.pdf"
    assert get(idb, "/invoices/5/versions/4/file").status_code == 404
    idb.idb.invoices[5]["vendor_id"] = "8"  # someone else's invoice
    idb.idb.commit()
    for path in ("/invoices/5/versions", "/invoices/5/versions/1", "/invoices/5/versions/1/file"):
        assert get(idb, path).status_code == 404


def test_routes_need_a_token(idb):
    assert idb.client.get("/invoices/5/versions").status_code == 401
