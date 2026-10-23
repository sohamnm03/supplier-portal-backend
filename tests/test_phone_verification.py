"""WhatsApp phone verification: sending the code, checking it, and the signup gate."""
import re
from datetime import datetime, timedelta

import pytest

import phone_verification as pv
import app as portal
from conftest import FakeResponse

PHONE = "+919998832823"
START = datetime(2026, 10, 23, 12, 0, 0)


class FakeTable:
    def __init__(self):
        self.now = START
        self.rows = {}


class FakeCursor:
    def __init__(self, table):
        self.t, self.rowcount, self._rows = table, 0, []

    def close(self):
        pass

    def fetchone(self):
        return self._rows[0] if self._rows else None

    def execute(self, sql, params=()):
        s, t, p = re.sub(r"\s+", " ", sql).strip(), self.t, tuple(params)
        self.rowcount, self._rows = 0, []
        if s.startswith("SELECT COUNT(*) AS sends"):
            rows = [r for r in t.rows.values() if r["phone"] == p[0] and r["created_at"] > t.now - timedelta(hours=1)]
            age = lambda r: int((t.now - r["created_at"]).total_seconds())  # noqa: E731
            self._rows = [{"sends": len(rows),
                           "since_last_s": min(map(age, rows)) if rows else None,
                           "since_first_s": max(map(age, rows)) if rows else None}]
        elif s.startswith("UPDATE phone_verification SET superseded_at"):
            for r in t.rows.values():
                if r["phone"] == p[0] and r["verified_at"] is None and r["superseded_at"] is None:
                    r["superseded_at"] = t.now
        elif s.startswith("INSERT INTO phone_verification"):
            vid, phone, code_hash, minutes = p
            t.rows[vid] = {"verification_id": vid, "phone": phone, "code_hash": code_hash, "attempts": 0,
                           "created_at": t.now, "expires_at": t.now + timedelta(minutes=minutes),
                           "verified_at": None, "superseded_at": None}
        elif s.startswith("SELECT verification_id, phone, code_hash"):
            r = t.rows.get(p[0])
            self._rows = [{**r, "live": r["expires_at"] > t.now}] if r else []
        elif s.startswith("UPDATE phone_verification SET attempts"):
            r = t.rows[p[0]]
            if r["attempts"] < p[1] and r["verified_at"] is None and r["superseded_at"] is None and r["expires_at"] > t.now:
                r["attempts"] += 1
                self.rowcount = 1
        elif s.startswith("UPDATE phone_verification SET verified_at"):
            r = t.rows[p[0]]
            if r["verified_at"] is None and r["superseded_at"] is None:
                r["verified_at"] = t.now
                self.rowcount = 1
        else:
            raise AssertionError(f"unexpected SQL: {s}")


class FakeConn:
    def __init__(self, table):
        self.table = table

    def cursor(self, dictionary=False):
        return FakeCursor(self.table)

    def commit(self):
        pass

    def close(self):
        pass


class Env:
    pass


@pytest.fixture
def env(monkeypatch):
    e = Env()
    e.table = FakeTable()
    e.sent = []  # (payload, headers) of every message handed to the WhatsApp service
    e.answer = FakeResponse({"success": True, "message_id": "wamid.1", "status": "accepted", "delivery_confirmed": False})
    monkeypatch.setattr(pv, "get_connection", lambda: FakeConn(e.table))
    monkeypatch.setenv("SHARED_SERVICE_API_KEY", "test-key")

    def post(url, json=None, headers=None, timeout=None):
        e.sent.append((url, json, headers))
        return e.answer

    monkeypatch.setattr(pv.requests, "post", post)
    portal.app.config["TESTING"] = True
    e.client = portal.app.test_client()

    def send(phone=PHONE):
        return e.client.post("/vendors/phone-verification/send", json={"phone": phone})

    def code():
        """The code the vendor would read on WhatsApp (the last one sent)."""
        return e.sent[-1][1]["template"]["components"][0]["parameters"][0]["text"]

    def verify(vid, otp):
        return e.client.post("/vendors/phone-verification/verify-otp", json={"verification_id": vid, "otp": otp})

    def wait(seconds):
        e.table.now += timedelta(seconds=seconds)

    e.send, e.code, e.verify, e.wait = send, code, verify, wait
    return e


def wrong(code):
    return "000000" if code != "000000" else "111111"


def test_send_hands_the_template_to_the_shared_service(env):
    r = env.send(" +91 99988-32823 ")
    body = r.get_json()
    assert r.status_code == 200 and body["success"] and body["verification_id"]
    url, payload, headers = env.sent[0]
    assert url.endswith("/api/whatsapp/messages") and headers == {"X-API-Key": "test-key"}
    code = env.code()
    assert re.fullmatch(r"\d{6}", code)
    assert payload == {
        "to": PHONE, "type": "template",
        "template": {"name": "fsverifyphone", "language": "en_US", "components": [
            {"type": "body", "parameters": [{"type": "text", "text": code}]},
            {"type": "button", "sub_type": "url", "index": "0", "parameters": [{"type": "text", "text": code}]},
        ]},
        "client_reference": f"dsa-otp-{body['verification_id']}",
    }


def test_the_code_is_never_stored(env):
    vid = env.send().get_json()["verification_id"]
    row = env.table.rows[vid]
    assert env.code() not in row.values() and len(row["code_hash"]) == 64


@pytest.mark.parametrize("phone", ["", "9998832823", "+0123456789", "+91abc", "+12345", None])
def test_send_rejects_numbers_that_are_not_international(env, phone):
    assert env.send(phone).status_code == 400
    assert env.sent == []


def test_send_without_an_api_key_is_unavailable_and_records_nothing(env, monkeypatch):
    monkeypatch.delenv("SHARED_SERVICE_API_KEY")
    assert env.send().status_code == 503
    assert env.sent == [] and env.table.rows == {}


@pytest.mark.parametrize("status,expected", [(400, 400), (401, 503), (502, 502)])
def test_a_refused_send_records_nothing_so_the_vendor_can_retry_at_once(env, status, expected):
    env.answer = FakeResponse({"success": False, "message": "no"}, status)
    assert env.send().status_code == expected
    assert env.table.rows == {}
    env.answer = FakeResponse({"status": "accepted"})
    assert env.send().status_code == 200


def test_a_response_without_accepted_is_not_a_sent_code(env):
    env.answer = FakeResponse({"success": True})
    assert env.send().status_code == 502 and env.table.rows == {}


def test_resend_cooldown_then_the_old_code_stops_working(env):
    first = env.send().get_json()["verification_id"]
    old_code = env.code()
    r = env.send()
    assert r.status_code == 429 and 0 < int(r.headers["Retry-After"]) <= pv.RESEND_SECONDS
    assert len(env.sent) == 1

    env.wait(pv.RESEND_SECONDS + 1)
    second = env.send().get_json()["verification_id"]
    assert second != first
    r = env.verify(first, old_code)
    assert r.status_code == 409 and r.get_json()["status"] == "superseded"
    assert env.verify(second, env.code()).get_json()["status"] == "verified"


def test_at_most_five_codes_an_hour_per_number(env):
    for _ in range(pv.MAX_SENDS_PER_HOUR):
        assert env.send().status_code == 200
        env.wait(pv.RESEND_SECONDS + 1)
    r = env.send()
    assert r.status_code == 429 and int(r.headers["Retry-After"]) > pv.RESEND_SECONDS
    assert env.send("+919876543210").status_code == 200  # another number is unaffected
    env.wait(3600)
    assert env.send().status_code == 200


def test_the_right_code_verifies_and_status_agrees(env):
    vid = env.send().get_json()["verification_id"]
    assert env.client.get(f"/vendors/phone-verification/{vid}/status").get_json()["status"] == "pending"
    r = env.verify(vid, env.code())
    assert r.status_code == 200 and r.get_json()["status"] == "verified"
    assert env.client.get(f"/vendors/phone-verification/{vid}/status").get_json()["status"] == "verified"
    assert env.verify(vid, wrong(env.code())).get_json()["status"] == "verified"  # already done: stays verified


def test_a_wrong_code_counts_down_and_five_misses_lock_it(env):
    vid = env.send().get_json()["verification_id"]
    bad = wrong(env.code())
    for left in (4, 3, 2, 1):
        r = env.verify(vid, bad)
        assert r.status_code == 400 and r.get_json()["attempts_remaining"] == left
    r = env.verify(vid, bad)
    assert r.status_code == 429 and r.get_json()["status"] == "locked"
    r = env.verify(vid, env.code())  # the right code is too late
    assert r.status_code == 429 and r.get_json()["status"] == "locked"
    assert env.client.get(f"/vendors/phone-verification/{vid}/status").get_json()["status"] == "locked"


def test_an_expired_code_is_refused(env):
    vid = env.send().get_json()["verification_id"]
    env.wait(pv.VALID_MINUTES * 60 + 1)
    r = env.verify(vid, env.code())
    assert r.status_code == 410 and r.get_json()["status"] == "expired"


@pytest.mark.parametrize("otp", ["", "12345", "1234567", "abcdef", "12 456"])
def test_a_malformed_code_is_rejected_without_using_an_attempt(env, otp):
    vid = env.send().get_json()["verification_id"]
    assert env.verify(vid, otp).status_code == 400
    assert env.table.rows[vid]["attempts"] == 0


def test_an_unknown_verification_is_not_found(env):
    assert env.verify("nope", "123456").status_code == 404
    assert env.client.get("/vendors/phone-verification/nope/status").status_code == 404


def test_verified_phone_error_needs_a_verified_code_for_this_exact_number(env):
    assert pv.verified_phone_error(None, PHONE)[1] == 403
    assert pv.verified_phone_error("nope", PHONE)[1] == 403
    vid = env.send().get_json()["verification_id"]
    assert pv.verified_phone_error(vid, PHONE)[1] == 403  # sent, not yet verified
    env.verify(vid, env.code())
    assert pv.verified_phone_error(vid, PHONE) is None
    assert pv.verified_phone_error(vid, "+91 99988 32823") is None  # spacing is not a different number
    assert pv.verified_phone_error(vid, "+919876543210")[1] == 403


def test_signup_needs_the_phone_verified(env, monkeypatch):
    monkeypatch.setattr(portal, "_verified_email_error", lambda vid, email: None)
    monkeypatch.setattr(portal, "_find_duplicate_identity", lambda fields: True)  # reaching it means the gate passed
    body = {"email": "v@vendor.test", "contact_no": PHONE, "verification_id": "email-vid"}

    r = env.client.post("/vendors/signup", json=body)
    assert r.status_code == 403 and "phone" in r.get_json()["error"].lower()

    vid = env.send().get_json()["verification_id"]
    assert env.client.post("/vendors/signup", json={**body, "phone_verification_id": vid}).status_code == 403

    env.verify(vid, env.code())
    assert env.client.post("/vendors/signup", json={**body, "phone_verification_id": vid}).status_code == 409
    other = {**body, "contact_no": "+919876543210", "phone_verification_id": vid}
    assert env.client.post("/vendors/signup", json=other).status_code == 403  # verified a different number
