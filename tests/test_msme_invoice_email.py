"""The MSME invoice email: sent in place of the generic "new invoice needs review" mail for an MSME vendor's invoice."""
import os
import sys
from datetime import date
from decimal import Decimal

import pytest
from mysql.connector import Error as MySQLError

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("DB_HOST", "x")
os.environ.setdefault("DB_USER", "x")
os.environ.setdefault("DB_PASSWORD", "x")
os.environ.setdefault("DB_NAME", "x")

import app as portal  # noqa: E402

VENDOR = {"vendor_legal_name": "Acme & Sons", "msme_status": "Registered", "udyam_number": "UDYAM-MH-12-0012345"}


@pytest.fixture
def sent(monkeypatch):
    calls = []

    def fake_send(to, subject, template_name, params, cc=None):
        calls.append({"to": to, "subject": subject, "template": template_name, "params": params, "cc": cc})
        return True

    monkeypatch.setattr(portal, "_send_email", fake_send)
    monkeypatch.setattr(portal, "_msme_recipients", lambda: ["maker@x.test", "checker@x.test"])
    return calls


def _send(vendor, **kwargs):
    portal._send_invoice_emails(
        "Acme & Sons", "vendor@x.test", "INV-2001", Decimal("4500000"), "INR", vendor=vendor, invoice_date=date(2026, 10, 22), **kwargs
    )


@pytest.mark.parametrize(
    "value, currency, expected",
    [
        (Decimal("1250000"), "INR", "₹12,50,000.00"),
        (Decimal("45000"), None, "₹45,000.00"),
        (Decimal("999.5"), "INR", "₹999.50"),
        (Decimal("12345678.9"), "INR", "₹1,23,45,678.90"),
        (Decimal("1250000"), "USD", "USD 1,250,000.00"),
    ],
)
def test_format_amount_groups_inr_in_lakhs_and_crores(value, currency, expected):
    assert portal._format_amount(value, currency) == expected


@pytest.mark.parametrize(
    "status, expected",
    [("Registered", "Not specified"), ("Small", "Small"), ("micro", "Micro"), ("Medium", "Medium"), ("Not registered", None), ("Not applicable", None), (None, None)],
)
def test_msme_category(status, expected):
    assert portal._msme_category(status) == expected


def test_an_msme_vendors_invoice_sends_the_msme_email_instead_of_the_generic_alert(sent):
    _send(VENDOR)

    assert [call["template"] for call in sent] == ["invoice_received", "invoice_msme_received"]
    receipt, msme = sent
    assert receipt["to"] == "vendor@x.test"
    assert msme["to"] == "maker@x.test" and msme["cc"] == ["checker@x.test"]
    assert msme["subject"] == "MSME invoice received: Acme & Sons INV-2001"
    # ven-shared-services 400s on any missing key, so the set must match the template exactly.
    assert set(msme["params"]) == {
        "vendor_name", "invoice_number", "invoice_amount", "msme_category", "udyam_number", "received_at", "payment_due_date", "review_url",
    }
    assert msme["params"]["invoice_amount"] == "₹45,00,000.00"
    assert msme["params"]["msme_category"] == "Not specified"
    assert msme["params"]["udyam_number"] == "UDYAM-MH-12-0012345"
    assert msme["params"]["payment_due_date"] == "06 Dec 2026"  # 45 days after the invoice date


def test_a_vendor_who_is_not_an_msme_gets_the_generic_alert_only(sent):
    _send({**VENDOR, "msme_status": "Not registered"})

    assert [call["template"] for call in sent] == ["invoice_received", "invoice_submitted"]


def test_without_a_vendor_row_nothing_changes(sent):
    _send(None)

    assert [call["template"] for call in sent] == ["invoice_received", "invoice_submitted"]


def test_if_the_msme_email_fails_the_maker_still_gets_the_generic_alert(monkeypatch, sent):
    monkeypatch.setattr(portal, "_send_email", lambda to, subject, template_name, params, cc=None: sent.append(template_name) or template_name != "invoice_msme_received")

    _send(VENDOR)

    assert sent == ["invoice_received", "invoice_msme_received", "invoice_submitted"]


def test_an_invoice_without_a_date_counts_the_deadline_from_today(sent, monkeypatch):
    class FixedDatetime(portal.datetime):
        @classmethod
        def now(cls, tz=None):
            return cls(2026, 11, 1, 10, 0, tzinfo=tz)

    monkeypatch.setattr(portal, "datetime", FixedDatetime)
    portal._send_invoice_emails("Acme", "vendor@x.test", "INV-1", Decimal("100"), "INR", vendor=VENDOR, invoice_date=None)

    assert sent[1]["params"]["payment_due_date"] == "16 Dec 2026"


class _FakeCursor:
    def __init__(self, rows=None, error=None):
        self.rows, self.error, self.executed = rows or [], error, []

    def execute(self, sql, params=None):
        if self.error:
            raise self.error
        self.executed.append(params)

    def fetchall(self):
        return self.rows

    def close(self):
        pass


class _FakeConn:
    def __init__(self, cursor):
        self._cursor = cursor

    def cursor(self):
        return self._cursor

    def close(self):
        pass


def test_recipients_come_from_the_configured_groups(monkeypatch):
    cursor = _FakeCursor(rows=[("checker@x.test",), ("maker@x.test",)])
    monkeypatch.setattr(portal, "get_connection", lambda: _FakeConn(cursor))

    assert portal._msme_recipients() == ["checker@x.test", "maker@x.test"]
    assert cursor.executed == [("INVOICE_MSME_RECEIVED",)]


@pytest.mark.parametrize("cursor", [_FakeCursor(rows=[]), _FakeCursor(error=MySQLError("no such table"))])
def test_recipients_fall_back_to_the_maker(monkeypatch, cursor):
    monkeypatch.setattr(portal, "get_connection", lambda: _FakeConn(cursor))

    assert portal._msme_recipients() == [portal.MAKER_EMAIL]
