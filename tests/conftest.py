"""Fixtures for the vendor-agreement tests: Flask test client, an in-memory stand-in for the few SQL statements the
agreement code runs, and fakes for the blob container, the email service and the email-verification service."""
import base64
import copy
import io
import json
import os
import re
import sys
from datetime import datetime, timedelta

import pytest
from PIL import Image, ImageDraw

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("DB_HOST", "x")
os.environ.setdefault("DB_USER", "x")
os.environ.setdefault("DB_PASSWORD", "x")
os.environ.setdefault("DB_NAME", "x")

import agreement_pdf  # noqa: E402,F401
import agreements  # noqa: E402
import app as portal  # noqa: E402
import auth  # noqa: E402

NOW = datetime(2026, 10, 20, 11, 42, 0)


def signature_png():
    image = Image.new("RGBA", (200, 80), (0, 0, 0, 0))
    ImageDraw.Draw(image).line([(10, 60), (80, 10), (150, 60)], fill=(10, 10, 80, 255), width=4)
    out = io.BytesIO()
    image.save(out, format="PNG")
    return out.getvalue()


def signature_data_url():
    return "data:image/png;base64," + base64.b64encode(signature_png()).decode()


def make_terms(no="AGR-2026-00001", version=1, start="2026-10-01", end="2027-09-30"):
    return {
        "agreement_no": no, "version": version, "agreement_date": "20 Oct 2026", "place": "Mumbai",
        "effective_date": start, "start_iso": start, "end_iso": end, "link_expires": "2026-11-03",
        "company": {"name": "Acme Ltd", "address": "1 Main Road", "signatory_name": "A Signatory",
                    "signatory_designation": "Director", "notice_email": "legal@acme.test"},
        "vendor": {"legal_name": "Vendor Pvt Ltd", "type": "Private Limited", "address": "2 Side Street",
                   "pan": "ABCDE1234F", "gstin": "27ABCDE1234F1Z5", "email": "v@vendor.test"},
        "terms": {},
        "clauses": [{"no": 1, "title": "Definitions", "body": "Some text."}, {"no": 2, "title": "Term", "body": "More text."}],
        "company_signature_b64": base64.b64encode(signature_png()).decode(),
    }


class FakeState:
    def __init__(self):
        self.now = NOW
        self.vendors = {7: {"vendor_id": 7, "email": "v@vendor.test", "status": "agreement_sent", "submission_round": 1,
                            "vendor_legal_name": "Vendor Pvt Ltd", "name": "V"}}
        self.agreements = {}
        self.otps = []
        self.events = []
        self.next_otp = 1

    def add_agreement(self, agreement_id, version, status="sent", vendor_id=7, end="2027-09-30", expires_in_days=10, **extra):
        no = f"AGR-2026-{agreement_id:05d}"
        row = {
            "agreement_id": agreement_id, "agreement_no": no, "vendor_id": vendor_id, "version": version, "status": status,
            "terms_json": json.dumps(make_terms(no, version, end=end)),
            "unsigned_pdf_path": f"vendor_{vendor_id}/agreement_{no}.pdf",
            "unsigned_pdf_sha256": f"{agreement_id:02d}" * 32,
            "signed_pdf_path": None, "signed_pdf_sha256": None, "issued_at": self.now - timedelta(days=2),
            "expires_at": self.now + timedelta(days=expires_in_days), "first_viewed_at": None, "signed_at": None,
            "signer_name": None, "declined_at": None, "decline_reason": None,
        }
        row.update(extra)
        self.agreements[agreement_id] = row
        return row


class FakeCursor:
    def __init__(self, db):
        self.db, self.rowcount, self._rows = db, 0, []

    def close(self):
        pass

    def fetchone(self):
        return self._rows[0] if self._rows else None

    def fetchall(self):
        return list(self._rows)

    def execute(self, sql, params=()):
        s, st, p = re.sub(r"\s+", " ", sql).strip(), self.db.state, tuple(params)
        self.rowcount, self._rows = 0, []
        if s.startswith("SELECT vendor_id, email, status, submission_round FROM vendor"):
            v = st.vendors.get(p[0])
            self._rows = [dict(v)] if v else []
        elif s.startswith("SELECT gstin FROM vendor"):
            self._rows = [{"gstin": st.vendors[p[0]].get("gstin")}]
        elif s.startswith("SELECT vendor_legal_name, name, email FROM vendor"):
            self._rows = [dict(st.vendors[p[0]])]
        elif s == "SELECT NOW() AS db_now":
            self._rows = [{"db_now": st.now}]
        elif "FROM vendor_agreement WHERE vendor_id = %s AND status IN" in s:
            rows = [r for r in st.agreements.values() if r["vendor_id"] == p[0] and r["status"] in ("sent", "signed", "declined")]
            rows.sort(key=lambda r: -r["version"])
            self._rows = [{**r, "start_iso": json.loads(r["terms_json"])["start_iso"],
                           "end_iso": json.loads(r["terms_json"])["end_iso"],
                           "key_terms_json": json.dumps(json.loads(r["terms_json"]).get("terms")),
                           "company_name": json.loads(r["terms_json"]).get("company", {}).get("name"),
                           "company_signatory_name": json.loads(r["terms_json"]).get("company", {}).get("signatory_name"),
                           "company_signatory_designation": json.loads(r["terms_json"]).get("company", {}).get("signatory_designation")} for r in rows]
        elif "FROM vendor_agreement WHERE vendor_id = %s ORDER BY version DESC LIMIT 1" in s:
            rows = sorted((r for r in st.agreements.values() if r["vendor_id"] == p[0]), key=lambda r: -r["version"])
            self._rows = [dict(rows[0])] if rows else []
        elif "FROM vendor_agreement WHERE vendor_id = %s ORDER BY version DESC" in s:
            rows = sorted((r for r in st.agreements.values() if r["vendor_id"] == p[0]), key=lambda r: -r["version"])
            self._rows = [dict(r) for r in rows]
        elif s.startswith("SELECT detail FROM agreement_event WHERE agreement_id"):
            rows = [e for e in st.events if e["agreement_id"] == p[0] and e["event_type"] == "declined"]
            self._rows = [{"detail": rows[-1]["detail"]}] if rows else []
        elif "FROM vendor_agreement WHERE agreement_id = %s AND vendor_id = %s" in s:
            r = st.agreements.get(p[0])
            self._rows = [{**r, "db_now": st.now}] if r and r["vendor_id"] == p[1] else []
        elif "FROM vendor_agreement WHERE agreement_no = %s" in s:
            r = next((a for a in st.agreements.values() if a["agreement_no"] == p[0]), None)
            self._rows = [{**r, "vendor_name": json.loads(r["terms_json"])["vendor"]["legal_name"]}] if r else []
        elif "FROM agreement_otp WHERE agreement_id" in s:
            rows = [o for o in st.otps if o["agreement_id"] == p[0]]
            if rows:
                o = rows[-1]
                self._rows = [{**o, "live": o["expires_at"] > st.now,
                               "age_s": int((st.now - o["created_at"]).total_seconds())}]
        elif s.startswith("INSERT INTO agreement_otp"):
            st.otps.append({"otp_id": st.next_otp, "agreement_id": p[0], "code_hash": p[1], "sent_to": p[2],
                            "expires_at": st.now + timedelta(minutes=p[3]), "attempts": 0, "used_at": None,
                            "created_at": st.now})
            st.next_otp += 1
        elif s.startswith("UPDATE agreement_otp SET attempts"):
            o = next(o for o in st.otps if o["otp_id"] == p[0])
            if o["attempts"] < p[1] and o["used_at"] is None:
                o["attempts"] += 1
                self.rowcount = 1
        elif s.startswith("UPDATE agreement_otp SET used_at"):
            o = next(o for o in st.otps if o["otp_id"] == p[0])
            if o["used_at"] is None:
                o["used_at"] = st.now
                self.rowcount = 1
        elif s.startswith("INSERT INTO agreement_event"):
            st.events.append({"agreement_id": p[0], "event_type": p[1], "actor": p[2],
                              "detail": json.loads(p[3]) if p[3] else None, "ip": p[4]})
        elif s.startswith("UPDATE vendor_agreement SET first_viewed_at"):
            r = st.agreements[p[0]]
            if r["first_viewed_at"] is None:
                r["first_viewed_at"] = st.now
                self.rowcount = 1
        elif s.startswith("UPDATE vendor_agreement SET status = 'signed'"):
            name, desig, method, ip, ua, path, sha, aid, vid = p
            r = st.agreements[aid]
            if r["vendor_id"] == vid and r["status"] == "sent" and (r["expires_at"] is None or r["expires_at"] >= st.now):
                r.update(status="signed", signed_at=st.now, signer_name=name, signer_designation=desig,
                         signature_method=method, signer_ip=ip, signer_user_agent=ua, signed_pdf_path=path,
                         signed_pdf_sha256=sha)
                self.rowcount = 1
        elif s.startswith("UPDATE vendor_agreement SET status = 'declined'"):
            reason, aid, vid = p
            r = st.agreements[aid]
            if r["vendor_id"] == vid and r["status"] == "sent" and (r["expires_at"] is None or r["expires_at"] >= st.now):
                r.update(status="declined", declined_at=st.now, decline_reason=reason)
                self.rowcount = 1
        elif s.startswith("UPDATE vendor SET status = %s, correction_fields"):
            new, keys, vid, old = p
            v = st.vendors[vid]
            if v["status"] == old:
                v.update(status=new, correction_fields=keys)
                self.rowcount = 1
        elif s.startswith("UPDATE vendor SET status"):
            new, vid, old = ("01", p[0], p[1]) if "'01'" in s else p
            v = st.vendors[vid]
            if v["status"] == old:
                v["status"] = new
                self.rowcount = 1
        else:
            raise AssertionError(f"unexpected SQL: {s}")


class FakeConn:
    def __init__(self, db):
        self.db = db

    def cursor(self, dictionary=False):
        return FakeCursor(self.db)

    def commit(self):
        self.db.commit_now()

    def rollback(self):
        self.db.state.__dict__.update(copy.deepcopy(self.db.snapshot))

    def close(self):
        self.rollback()  # like MySQL, an uncommitted transaction is discarded


class FakeDb:
    def __init__(self):
        self.state = FakeState()
        self.commit_now()

    def commit_now(self):
        self.snapshot = copy.deepcopy(self.state.__dict__)

    def connect(self):
        return FakeConn(self)


class FakeContainer:
    def __init__(self):
        self.blobs, self.uploads, self.deleted, self.metadata = {}, [], [], {}

    def upload_blob(self, name, data, overwrite=False, content_settings=None, metadata=None):
        self.blobs[name] = data
        self.metadata[name] = metadata
        self.uploads.append(name)

    def delete_blob(self, name):
        self.blobs.pop(name, None)
        self.deleted.append(name)

    def get_blob_client(self, name):
        container = self

        class Client:
            def download_blob(self):
                class Download:
                    def readall(_self):
                        return container.blobs[name]
                return Download()
        return Client()


class FakeResponse:
    def __init__(self, body, status=200, headers=None):
        self.body, self.status_code, self.headers = body, status, headers or {}
        self.ok = status < 400

    def json(self):
        return self.body


class Env:
    pass


@pytest.fixture
def env(monkeypatch):
    e = Env()
    e.db = FakeDb()
    e.state = e.db.state
    e.container = FakeContainer()
    e.emails = []
    e.verification_calls = []
    e.verify_answer = FakeResponse({"success": True})
    e.verified_error = None  # what the service's status says: None = verified
    monkeypatch.setattr(agreements, "get_connection", e.db.connect)
    monkeypatch.setattr(auth, "get_connection", e.db.connect)

    def ev_request(method, path, **kwargs):
        e.verification_calls.append((method, path, kwargs))
        if path == "/send":
            return FakeResponse({"success": True, "verification_id": f"vid-{len(e.verification_calls)}"})
        return e.verify_answer

    monkeypatch.setitem(agreements.deps, "email_verification_request", ev_request)
    monkeypatch.setitem(agreements.deps, "verified_email_error", lambda vid, email: e.verified_error)
    monkeypatch.setitem(agreements.deps, "docs_container", lambda: e.container)
    monkeypatch.setitem(agreements.deps, "send_email",
                        lambda to, subject, template, params: e.emails.append((to, subject, template, params)) or True)
    e.change_emails = []
    monkeypatch.setitem(agreements.deps, "field_master", lambda has_gstin: {"pan": {"field_key": "pan", "field_label": "PAN"}, "ifsc": {"field_key": "ifsc", "field_label": "IFSC"}})
    monkeypatch.setitem(agreements.deps, "change_request_email", lambda vendor, status, labels: e.change_emails.append((status, labels)))
    monkeypatch.setitem(agreements.deps, "group_emails", lambda code, fallback: ["checker1@acme.test", "checker2@acme.test"])
    for name in ("VENDOR_AGREEMENT_SIGNED_TEMPLATE", "VENDOR_AGREEMENT_SIGNED_NOTICE_TEMPLATE",
                 "VENDOR_AGREEMENT_DECLINED_TEMPLATE"):
        monkeypatch.delenv(name, raising=False)
    portal.app.config["TESTING"] = True
    e.client = portal.app.test_client()
    e.headers = {"Authorization": "Bearer " + auth.issue_token(7, "v@vendor.test")}

    def sign_body(**over):
        body = {"signer_name": "Vee Vendor", "signer_designation": "Director", "method": "draw",
                "signature": signature_data_url(), "details_confirmed": True}
        body.update(over)
        return body

    e.sign_body = sign_body

    def send_code(agreement_id=1):
        r = e.client.post(f"/vendors/me/agreements/{agreement_id}/send-code", headers=e.headers)
        assert r.status_code == 200, r.get_json()
        e.state.now += timedelta(seconds=61)
        e.db.commit_now()
        return r

    e.send_code = send_code
    return e
