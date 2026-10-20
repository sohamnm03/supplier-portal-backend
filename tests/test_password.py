"""POST /vendors/me/password: a signed-in vendor changes their own password."""
import hashlib
import re

import pytest

import app as portal
import auth


def sha(text):
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


class PasswordCursor:
    def __init__(self, store):
        self.store, self.rowcount, self._rows = store, 0, []

    def close(self):
        pass

    def fetchone(self):
        return self._rows[0] if self._rows else None

    def execute(self, sql, params=()):
        s = re.sub(r"\s+", " ", sql).strip()
        if s == "SELECT password FROM vendor WHERE vendor_id = %s":
            self._rows = [{"password": self.store["password"]}]
        elif s == "UPDATE vendor SET password = %s WHERE vendor_id = %s":
            assert params[1] == 7
            self.store["password"] = params[0]
            self.rowcount = 1
        else:
            raise AssertionError(f"unexpected SQL: {s}")


class PasswordConn:
    def __init__(self, store):
        self.store, self.commits = store, 0

    def cursor(self, dictionary=False):
        return PasswordCursor(self.store)

    def commit(self):
        self.store["commits"] = self.store.get("commits", 0) + 1

    def close(self):
        pass


@pytest.fixture
def pw(env, monkeypatch):
    env.store = {"password": sha("Emailed#123")}
    monkeypatch.setattr(portal, "get_connection", lambda: PasswordConn(env.store))
    auth._failures.clear()
    yield env
    auth._failures.clear()


def post(env, **body):
    return env.client.post("/vendors/me/password", headers=env.headers, json=body)


def test_changes_password(pw):
    r = post(pw, current_password="Emailed#123", new_password="Brand-new-pass1")
    assert r.status_code == 200
    assert pw.store["password"] == sha("Brand-new-pass1")
    assert pw.store["commits"] == 1


def test_works_in_agreement_and_active_stage(pw):
    for status in ("agreement_sent", "agreement_signed", "active", "01"):
        pw.state.vendors[7]["status"] = status
        pw.store["password"] = sha("Emailed#123")
        assert post(pw, current_password="Emailed#123", new_password="Another-pass-9").status_code == 200


def test_rejected_vendor_refused(pw):
    pw.state.vendors[7]["status"] = "Rejected"
    r = post(pw, current_password="Emailed#123", new_password="Another-pass-9")
    assert r.status_code == 403 and r.get_json()["code"] == "stage_not_allowed"
    assert pw.store["password"] == sha("Emailed#123")


def test_needs_token(pw):
    assert pw.client.post("/vendors/me/password", json={}).status_code == 401


def test_wrong_current_password(pw):
    r = post(pw, current_password="nope-nope", new_password="Brand-new-pass1")
    assert r.status_code == 403 and r.get_json()["error"] == "Current password is incorrect"
    assert pw.store["password"] == sha("Emailed#123")


@pytest.mark.parametrize("new,fragment", [
    ("short", "8 to 64"), ("x" * 65, "8 to 64"), ("Emailed#123", "different"),
])
def test_new_password_rules(pw, new, fragment):
    r = post(pw, current_password="Emailed#123", new_password=new)
    assert r.status_code == 400 and fragment in r.get_json()["error"]
    assert pw.store["password"] == sha("Emailed#123")


def test_missing_fields(pw):
    assert post(pw, new_password="Brand-new-pass1").status_code == 400
    assert post(pw, current_password="Emailed#123").status_code == 400
    assert pw.client.post("/vendors/me/password", headers=pw.headers, data="x").status_code == 400


def test_throttled_after_repeated_wrong_current_password(pw):
    for _ in range(5):
        assert post(pw, current_password="wrong-pass", new_password="Brand-new-pass1").status_code == 403
    r = post(pw, current_password="Emailed#123", new_password="Brand-new-pass1")
    assert r.status_code == 429 and int(r.headers["Retry-After"]) > 0
    assert pw.store["password"] == sha("Emailed#123")


def test_success_clears_failures(pw):
    post(pw, current_password="wrong-pass", new_password="Brand-new-pass1")
    assert post(pw, current_password="Emailed#123", new_password="Brand-new-pass1").status_code == 200
    assert not auth._failures
