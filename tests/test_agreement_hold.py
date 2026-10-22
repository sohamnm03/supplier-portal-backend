"""The agreement hold (agreement_hold.py), its enforcement on the vendor's write routes and the renewal request."""
import json
from datetime import date

import pytest
from flask import g

import agreement_hold
import app as portal

TODAY = date(2026, 10, 20)


def row(start, end):
    return {"start_iso": start, "end_iso": end}


def hold(status="active", rows=(), release=None, enabled=True, today=TODAY):
    return agreement_hold.evaluate(status, list(rows), release, today, enabled)


# --- the rule ---------------------------------------------------------------------------------------------

def test_no_agreement_never_on_hold():
    assert hold(rows=[])["on_hold"] is False


def test_in_force_not_on_hold():
    assert hold(rows=[row("2026-01-01", "2027-01-01")])["on_hold"] is False


def test_expired_on_hold():
    state = hold(rows=[row("2025-01-01", "2026-10-19")])
    assert state["on_hold"] is True and state["reason"] == "expired" and state["expired_on"] == "2026-10-19" and state["next_start"] is None


def test_last_and_first_day_are_inclusive():
    assert hold(rows=[row("2025-01-01", "2026-10-20")])["on_hold"] is False
    assert hold(rows=[row("2026-10-20", "2027-10-20")])["on_hold"] is False
    assert hold(rows=[row("2026-10-21", "2027-10-20")])["on_hold"] is True


def test_between_agreements():
    state = hold(rows=[row("2027-01-01", "2028-01-01"), row("2025-01-01", "2026-09-30")])
    assert state["on_hold"] is True and state["reason"] == "between_agreements"
    assert state["next_start"] == "2027-01-01" and state["expired_on"] == "2028-01-01"


def test_a_scheduled_renewal_does_not_matter_while_one_is_in_force():
    assert hold(rows=[row("2027-01-01", "2028-01-01"), row("2026-01-01", "2026-12-31")])["on_hold"] is False


@pytest.mark.parametrize("status", ["Pending", "01", "agreement_sent", "agreement_signed", "returned"])
def test_only_active_vendors_can_be_on_hold(status):
    assert hold(status=status, rows=[row("2025-01-01", "2026-01-01")])["on_hold"] is False


def test_status_is_case_insensitive():
    assert hold(status="Active", rows=[row("2025-01-01", "2026-01-01")])["on_hold"] is True


def test_setting_off_never_holds(monkeypatch):
    assert hold(rows=[row("2025-01-01", "2026-01-01")], enabled=False)["on_hold"] is False
    monkeypatch.setenv("AGREEMENT_EXPIRY_HOLD", "false")
    assert agreement_hold.enabled() is False
    monkeypatch.setenv("AGREEMENT_EXPIRY_HOLD", "true")
    assert agreement_hold.enabled() is True


def test_release_lifts_the_hold_until_its_date():
    expired = [row("2025-01-01", "2026-01-01")]
    active = {"event_type": "hold_released", "detail": {"until": "2026-10-20", "reason": "renewal in progress"}}
    state = hold(rows=expired, release=active)
    assert state["on_hold"] is False and state["released_until"] == "2026-10-20" and state["reason"] == "expired"
    gone = {"event_type": "hold_released", "detail": json.dumps({"until": "2026-10-19"})}
    assert hold(rows=expired, release=gone)["on_hold"] is True
    ended = {"event_type": "hold_release_ended", "detail": {"until": "2027-01-01"}}
    assert hold(rows=expired, release=ended)["on_hold"] is True


def test_expiry_of_the_agreement_in_force():
    assert agreement_hold.expiry_of([row("2026-01-01", "2026-11-15")], TODAY) == {"end_date": "2026-11-15", "days_left": 26, "expiring": True}
    far = agreement_hold.expiry_of([row("2026-01-01", "2027-10-20")], TODAY)
    assert far["days_left"] == 365 and far["expiring"] is False
    assert agreement_hold.expiry_of([row("2026-01-01", "2026-12-19")], TODAY)["expiring"] is True  # exactly 60 days
    assert agreement_hold.expiry_of([row("2025-01-01", "2026-01-01")], TODAY) is None
    assert agreement_hold.expiry_of([], TODAY) is None


# --- from the database ------------------------------------------------------------------------------------

def signed(env, agreement_id, version, start, end):
    r = env.state.add_agreement(agreement_id, version, "signed", end=end, signed_pdf_path="p")
    terms = json.loads(r["terms_json"])
    terms["start_iso"] = start
    r["terms_json"] = json.dumps(terms)
    return r


def activate(env):
    env.state.vendors[7]["status"] = "active"
    env.db.commit_now()


def test_state_for_reads_the_dates_and_the_release(env):
    activate(env)
    signed(env, 1, 1, "2025-01-01", "2026-09-30")
    env.db.commit_now()
    state, expiry = agreement_hold.state_for(7, "active")
    assert state["on_hold"] is True and state["expired_on"] == "2026-09-30" and expiry is None
    env.state.events.append({"agreement_id": 1, "event_type": "hold_released", "actor": "c", "detail": {"until": "2026-11-01"}, "ip": None})
    env.db.commit_now()
    state, _ = agreement_hold.state_for(7, "active")
    assert state["on_hold"] is False and state["released_until"] == "2026-11-01"


# --- enforcement ------------------------------------------------------------------------------------------

WRITES = [
    ("post", "/invoices/ocr"),
    ("post", "/invoices/5/replace"),
    ("post", "/invoices/5/replace-preview"),
    ("post", "/vendors/update-requests"),
]


@pytest.mark.parametrize("method,path", WRITES)
def test_writes_are_refused_with_423_while_on_hold(env, method, path):
    activate(env)
    signed(env, 1, 1, "2025-01-01", "2026-09-30")
    env.db.commit_now()
    r = getattr(env.client, method)(path, headers=env.headers, json={})
    body = r.get_json()
    assert r.status_code == 423 and body["code"] == "agreement_expired" and body["reason"] == "expired" and body["expired_on"] == "2026-09-30"
    assert body["error"] == "Your agreement has expired. Actions are on hold until a new agreement is signed."


def test_reads_and_agreement_routes_stay_open_while_on_hold(env):
    activate(env)
    signed(env, 1, 1, "2025-01-01", "2026-09-30")
    env.db.commit_now()
    assert env.client.get("/vendors/me/agreements", headers=env.headers).status_code == 200
    assert env.client.get("/vendors/me/renewal", headers=env.headers).status_code == 200


def test_guard_decision(env):
    @agreement_hold.write_guard
    def view():
        return "ok"

    activate(env)
    signed(env, 1, 1, "2025-01-01", "2026-09-30")
    env.db.commit_now()
    with portal.app.test_request_context():
        g.vendor = {"vendor_id": 7, "status": "active"}
        response, status = view()
        assert status == 423
    env.state.events.append({"agreement_id": 1, "event_type": "hold_released", "actor": "c", "detail": {"until": "2026-10-25"}, "ip": None})
    env.db.commit_now()
    with portal.app.test_request_context():
        g.vendor = {"vendor_id": 7, "status": "active"}
        assert view() == "ok"
    with portal.app.test_request_context():
        g.vendor = {"vendor_id": 7, "status": "01"}
        assert view() == "ok"


def test_guard_ignores_vendor_in_force(env):
    activate(env)
    signed(env, 1, 1, "2026-01-01", "2027-09-30")
    env.db.commit_now()
    state, expiry = agreement_hold.state_for(7, "active")
    assert state["on_hold"] is False and expiry["days_left"] > 60


# --- renewal request --------------------------------------------------------------------------------------

def renewal(env):
    return env.client.get("/vendors/me/renewal", headers=env.headers).get_json()


def ask(env, **body):
    return env.client.post("/vendors/me/renewal-request", headers=env.headers, json=body)


def test_eligibility(env):
    activate(env)
    assert renewal(env)["can_request"] is False  # nothing signed
    assert ask(env, term_months=12).status_code == 409
    signed(env, 1, 1, "2026-01-01", "2027-09-30")  # 345 days left
    env.db.commit_now()
    assert renewal(env)["can_request"] is False
    assert ask(env, term_months=12).status_code == 409
    env.state.agreements[1]["terms_json"] = json.dumps({**json.loads(env.state.agreements[1]["terms_json"]), "end_iso": "2026-12-19"})
    env.db.commit_now()
    assert renewal(env)["can_request"] is True  # 60 days left


def test_window_while_ending_and_after_expiry(env):
    activate(env)
    signed(env, 1, 1, "2026-01-01", "2026-11-15")
    env.db.commit_now()
    info = renewal(env)
    assert info["min_start"] == "2026-11-16" and info["max_start"] == "2027-12-21" and info["current_end"] == "2026-11-15"
    assert info["default_term_months"] == 24 and info["open_request"] is None and info["last_outcome"] is None
    env.state.agreements[1]["terms_json"] = json.dumps({**json.loads(env.state.agreements[1]["terms_json"]), "end_iso": "2026-09-30"})
    env.db.commit_now()
    info = renewal(env)
    assert info["can_request"] is True and info["min_start"] == "2026-10-20"  # on hold: from today


def ending(env):
    activate(env)
    signed(env, 1, 1, "2026-01-01", "2026-11-15")
    env.db.commit_now()


@pytest.mark.parametrize("body,fragment", [
    ({"start_date": "2026-11-15"}, "only after the current agreement ends"),
    ({"start_date": "2028-01-01"}, "at most 400 days"),
    ({"start_date": "next week"}, "must be a date"),
    ({"term_months": 0}, "from 1 to 60"),
    ({"term_months": 61}, "from 1 to 60"),
    ({"term_months": "12"}, "from 1 to 60"),
    ({"note": "x" * 501}, "at most 500"),
])
def test_request_rules(env, body, fragment):
    ending(env)
    r = ask(env, **body)
    assert r.status_code == 400 and fragment in r.get_json()["error"]
    assert not [e for e in env.state.events if e["event_type"] == "renewal_requested"]


def test_request_then_one_open_then_cancel(env, monkeypatch):
    monkeypatch.setenv("AGREEMENT_RENEWAL_REQUESTED_TEMPLATE", "t_renewal")
    ending(env)
    r = ask(env, start_date="2026-12-01", term_months=12, note="  Our bank changed  ")
    assert r.status_code == 201
    body = r.get_json()
    assert body["can_request"] is False
    assert body["open_request"]["start_date"] == "2026-12-01" and body["open_request"]["term_months"] == 12
    assert body["open_request"]["note"] == "Our bank changed"
    event = env.state.events[-1]
    assert event["event_type"] == "renewal_requested" and event["agreement_id"] == 1
    assert event["detail"] == {"start_date": "2026-12-01", "term_months": 12, "note": "Our bank changed", "requested_by": "v@vendor.test"}
    assert sorted(m[0] for m in env.emails) == ["checker1@acme.test", "checker2@acme.test"]
    assert env.emails[0][3] == {"vendor_name": "Vendor Pvt Ltd", "requested_start": "2026-12-01", "term_months": 12, "note": "Our bank changed"}
    assert ask(env, term_months=12).status_code == 409  # one open request at a time

    cancelled = env.client.delete("/vendors/me/renewal-request", headers=env.headers)
    assert cancelled.status_code == 200
    info = cancelled.get_json()
    assert info["open_request"] is None and info["can_request"] is True and info["last_outcome"]["status"] == "cancelled"
    assert env.state.events[-1]["event_type"] == "renewal_request_cancelled"
    assert env.client.delete("/vendors/me/renewal-request", headers=env.headers).status_code == 409


def test_defaults_when_nothing_is_sent(env):
    ending(env)
    r = ask(env)
    assert r.status_code == 201
    assert r.get_json()["open_request"]["start_date"] == "2026-11-16" and r.get_json()["open_request"]["term_months"] == 24


def test_outcomes_written_by_the_checkers(env):
    ending(env)
    ask(env, term_months=12)
    env.state.events.append({"agreement_id": 1, "event_type": "renewal_request_declined", "actor": "c", "detail": {"reason": "Not yet"},
                             "ip": None, "created_at": env.state.now})
    env.db.commit_now()
    info = renewal(env)
    assert info["open_request"] is None and info["can_request"] is True
    assert info["last_outcome"]["status"] == "declined" and info["last_outcome"]["reason"] == "Not yet"
    ask(env, term_months=6)
    env.state.events.append({"agreement_id": 1, "event_type": "renewal_request_fulfilled", "actor": "c", "detail": {"agreement_id": 2},
                             "ip": None, "created_at": env.state.now})
    env.db.commit_now()
    assert renewal(env)["last_outcome"]["status"] == "fulfilled"


def test_renewal_routes_are_for_active_vendors(env):
    env.state.vendors[7]["status"] = "01"
    assert env.client.get("/vendors/me/renewal", headers=env.headers).status_code == 403
    assert env.client.post("/vendors/me/renewal-request", headers=env.headers, json={}).status_code == 403
