"""PAN and email duplicate rules: only submitted applications claim a PAN; any real account holds its email; drafts and rejected rows hold neither."""
import re

import pytest

import app as portal
import auth
import verification

CLAIMING = ["Pending", "01", "agreement_sent", "agreement_signed", "02", "active", "returned", "returned_to_maker", "change_req_maker", "change_req_checker"]


class DupCursor:
    """Evaluates the duplicate queries against in-memory rows, following the SQL it is given (the rule fragments are matched exactly)."""

    def __init__(self, rows):
        self.rows, self._found = rows, None

    def close(self):
        pass

    def fetchone(self):
        return self._found

    def execute(self, sql, params=()):
        s = re.sub(r"\s+", " ", sql).strip()
        match = re.match(r"SELECT vendor_id FROM vendor WHERE (UPPER\(TRIM\(pan\)\)|LOWER\(TRIM\(email\)\)) = %s(?: AND vendor_id <> %s)? AND (.+) LIMIT 1$", s)
        assert match, f"unexpected SQL: {s}"
        column = "pan" if "pan" in match.group(1) else "email"
        rule = match.group(2)
        value = params[0]
        exclude = params[1] if len(params) > 1 else None
        if rule == "is_draft = 0 AND LOWER(status) NOT IN ('registered', 'rejected')":
            holds = lambda row: not row["is_draft"] and row["status"].lower() not in ("registered", "rejected")
        elif rule == "is_draft = 0 AND LOWER(status) <> 'rejected'":
            holds = lambda row: not row["is_draft"] and row["status"].lower() != "rejected"
        else:
            raise AssertionError(f"unexpected rule: {rule}")
        norm = (lambda v: str(v or "").strip().upper()) if column == "pan" else (lambda v: str(v or "").strip().lower())
        self._found = next(({"vendor_id": r["vendor_id"]} for r in self.rows
                            if norm(r[column]) == value and r["vendor_id"] != exclude and holds(r)), None)


class DupConn:
    def __init__(self, rows):
        self.rows = rows

    def cursor(self, dictionary=False):
        return DupCursor(self.rows)

    def close(self):
        pass


@pytest.fixture
def rows(monkeypatch):
    data = []
    monkeypatch.setattr(portal, "get_connection", lambda: DupConn(data))
    return data


@pytest.fixture(autouse=True)
def pan_check_on(monkeypatch):
    """The PAN duplicate check is switched OFF by default for now (VENDOR_PAN_DUPLICATE_CHECK); these tests cover the rule itself, so they run
    with it ON. The tests of the OFF behaviour at the end of the file remove the variable again."""
    monkeypatch.setenv("VENDOR_PAN_DUPLICATE_CHECK", "true")


def add(rows, vendor_id=50, pan="ABCDE1234F", email="x@y.test", status="Pending", is_draft=0):
    rows.append({"vendor_id": vendor_id, "pan": pan, "email": email, "status": status, "is_draft": is_draft})


# --- the rules ---------------------------------------------------------------------------------------------

@pytest.mark.parametrize("status", CLAIMING)
def test_every_claiming_status_blocks_the_pan(rows, status):
    add(rows, status=status)
    assert portal._find_duplicate_identity({"pan": " abcde1234f "}) == "A vendor with this PAN already exists."


@pytest.mark.parametrize("status,is_draft", [("Pending", 1), ("registered", 0), ("Registered", 0), ("Rejected", 0), ("rejected", 1)])
def test_drafts_signups_and_rejected_do_not_claim_the_pan(rows, status, is_draft):
    add(rows, status=status, is_draft=is_draft)
    assert portal._find_duplicate_identity({"pan": "ABCDE1234F"}) is None


def test_email_is_held_by_a_signed_up_account_but_not_by_a_draft_or_rejected(rows):
    add(rows, status="registered")
    assert portal._find_duplicate_identity({"email": "X@Y.test"}) == "A vendor with this email already exists."
    rows.clear()
    add(rows, status="Pending", is_draft=1)
    assert portal._find_duplicate_identity({"email": "x@y.test"}) is None
    rows.clear()
    add(rows, status="Rejected")
    assert portal._find_duplicate_identity({"email": "x@y.test"}) is None
    rows.clear()
    add(rows, status="active")
    assert portal._find_duplicate_identity({"email": "x@y.test"}) is not None


def test_own_row_is_excluded(rows):
    add(rows, vendor_id=7, status="Pending")
    assert portal._find_duplicate_identity({"pan": "ABCDE1234F"}, exclude_vendor_id=7) is None
    assert portal._find_duplicate_identity({"pan": "ABCDE1234F"}, exclude_vendor_id=8) is not None


# --- routes --------------------------------------------------------------------------------------------------

def test_check_pan_route(rows, env):
    client = portal.app.test_client()
    add(rows, vendor_id=50, status="Pending", is_draft=1)
    assert client.post("/vendors/check-pan", json={"pan": "abcde1234f"}).get_json()["exists"] is False
    add(rows, vendor_id=51, status="registered")
    assert client.post("/vendors/check-pan", json={"pan": "ABCDE1234F"}).get_json()["exists"] is False
    add(rows, vendor_id=52, status="01")
    assert client.post("/vendors/check-pan", json={"pan": "ABCDE1234F"}).get_json()["exists"] is True
    # the caller's own row never counts
    token = {"Authorization": "Bearer " + auth.issue_token(52, "x@y.test")}
    assert client.post("/vendors/check-pan", json={"pan": "ABCDE1234F"}, headers=token).get_json()["exists"] is False


def test_check_email_route(rows, env):
    client = portal.app.test_client()
    add(rows, status="Pending", is_draft=1)
    assert client.post("/vendors/check-email", json={"email": "x@y.test"}).get_json()["exists"] is False
    add(rows, vendor_id=51, status="registered")
    assert client.post("/vendors/check-email", json={"email": "X@y.test"}).get_json()["exists"] is True


def test_submit_is_strict_but_ignores_drafts(rows, env, monkeypatch):
    env.state.vendors[7]["status"] = "registered"
    env.db.commit_now()
    full = {field: "x" for field in portal.ONBOARDING_FIELDS}
    full.update(pan="ABCDE1234F", email="v@vendor.test")
    monkeypatch.setattr(portal, "_load_vendor_row", lambda vendor_id: dict(full))
    monkeypatch.setattr(portal, "_validate_vendor_values", lambda values, **kwargs: None)
    monkeypatch.setattr(verification, "enforce_on_submit", lambda values: ("stopped here", 418))

    add(rows, vendor_id=50, status="Pending", is_draft=1)
    add(rows, vendor_id=51, status="registered")
    r = env.client.post("/vendors/me/submit", headers=env.headers, json={"details": {}})
    assert r.status_code == 418  # got past the duplicate check

    add(rows, vendor_id=52, status="Pending")  # first to submit wins
    r = env.client.post("/vendors/me/submit", headers=env.headers, json={"details": {}})
    assert r.status_code == 409 and r.get_json()["error"] == "A vendor with this PAN already exists."


# --- the temporary switch (VENDOR_PAN_DUPLICATE_CHECK) ---------------------------------------------------------

@pytest.mark.parametrize("value,expected", [("true", True), ("1", True), ("Yes", True), ("ON", True), ("false", False), ("", False), ("0", False)])
def test_the_switch_is_parsed(monkeypatch, value, expected):
    monkeypatch.setenv("VENDOR_PAN_DUPLICATE_CHECK", value)
    assert portal.pan_duplicate_check_enabled() is expected


def test_the_switch_is_off_by_default(monkeypatch):
    monkeypatch.delenv("VENDOR_PAN_DUPLICATE_CHECK", raising=False)
    assert portal.pan_duplicate_check_enabled() is False


def test_with_the_switch_off_a_used_pan_is_not_refused_but_the_email_still_is(rows, monkeypatch):
    monkeypatch.delenv("VENDOR_PAN_DUPLICATE_CHECK", raising=False)
    add(rows, status="active")
    assert portal._find_duplicate_identity({"pan": "ABCDE1234F"}) is None
    assert portal._find_duplicate_identity({"pan": "ABCDE1234F", "email": "x@y.test"}) == "A vendor with this email already exists."


def test_with_the_switch_off_check_pan_reports_no_duplicate(rows, env, monkeypatch):
    monkeypatch.delenv("VENDOR_PAN_DUPLICATE_CHECK", raising=False)
    add(rows, vendor_id=52, status="01")
    client = portal.app.test_client()
    assert client.post("/vendors/check-pan", json={"pan": "ABCDE1234F"}).get_json()["exists"] is False


def test_with_the_switch_off_submit_passes_the_duplicate_step_even_when_a_submitted_vendor_holds_the_pan(rows, env, monkeypatch):
    monkeypatch.delenv("VENDOR_PAN_DUPLICATE_CHECK", raising=False)
    env.state.vendors[7]["status"] = "registered"
    env.db.commit_now()
    full = {field: "x" for field in portal.ONBOARDING_FIELDS}
    full.update(pan="ABCDE1234F", email="v@vendor.test")
    monkeypatch.setattr(portal, "_load_vendor_row", lambda vendor_id: dict(full))
    monkeypatch.setattr(portal, "_validate_vendor_values", lambda values, **kwargs: None)
    monkeypatch.setattr(verification, "enforce_on_submit", lambda values: ("stopped here", 418))
    add(rows, vendor_id=52, status="Pending")
    r = env.client.post("/vendors/me/submit", headers=env.headers, json={"details": {}})
    assert r.status_code == 418  # got past the duplicate check although another submitted vendor holds the PAN
