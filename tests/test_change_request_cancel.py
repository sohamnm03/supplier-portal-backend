"""Withdrawing a change request raised by "Something is wrong" at the agreement's details review restores that very agreement."""
import pytest

import agreements
import app as portal


@pytest.fixture
def cr(env, monkeypatch):
    monkeypatch.setattr(portal, "get_connection", env.db.connect)
    monkeypatch.setattr(portal, "_vendor_with_progress", lambda vendor_id: {"vendor_id": vendor_id})
    env.state.vendors[7].update(status="change_req_checker", correction_fields="pan,gstin")
    env.db.commit_now()
    return env


def declined(env, step="review", expires_in_days=10, version=1):
    env.state.add_agreement(1, version, "declined", expires_in_days=expires_in_days, decline_reason="Details need to be corrected.",
                            declined_at=env.state.now)
    env.state.events.append({"agreement_id": 1, "event_type": "declined", "actor": "v@vendor.test", "ip": None, "created_at": env.state.now,
                             "detail": {"reason": "Details need to be corrected.", "at_step": step, "fields": ["pan", "gstin"]}})
    env.db.commit_now()


def cancel(env):
    return env.client.post("/vendors/me/change-request/cancel", headers=env.headers)


def test_review_step_decline_is_restored_to_the_same_agreement(cr):
    declined(cr)
    assert cancel(cr).status_code == 200
    vendor, agreement = cr.state.vendors[7], cr.state.agreements[1]
    assert vendor["status"] == "agreement_sent" and vendor["correction_fields"] is None
    assert agreement["status"] == "sent" and agreement["declined_at"] is None and agreement["decline_reason"] is None
    assert agreement["agreement_id"] == 1  # the SAME row, not a new version
    event = cr.state.events[-1]
    assert event["event_type"] == "change_request_cancelled" and event["agreement_id"] == 1
    assert event["detail"] == {"previous_decline_reason": "Details need to be corrected."}
    # the vendor sees the agreement waiting for their signature again
    assert cr.client.get("/vendors/me/agreements", headers=cr.headers).get_json()[0]["can_sign"] is True


def test_expired_signing_link_falls_back_to_the_ordinary_withdraw(cr):
    declined(cr, expires_in_days=-1)
    assert cancel(cr).status_code == 200
    assert cr.state.vendors[7]["status"] == "01" and cr.state.vendors[7]["correction_fields"] in (None, "pan,gstin")
    assert cr.state.agreements[1]["status"] == "declined"
    assert not [e for e in cr.state.events if e["event_type"] == "change_request_cancelled"]


def test_a_change_request_not_raised_from_the_agreement_is_unchanged(cr):
    assert cancel(cr).status_code == 200  # no agreement at all
    assert cr.state.vendors[7]["status"] == "01"
    cr.state.vendors[7]["status"] = "change_req_maker"
    cr.db.commit_now()
    assert cancel(cr).status_code == 200
    assert cr.state.vendors[7]["status"] == "Pending"


def test_a_sign_step_decline_is_unaffected(cr):
    declined(cr, step="sign")
    assert cancel(cr).status_code == 200
    assert cr.state.vendors[7]["status"] == "01"
    assert cr.state.agreements[1]["status"] == "declined"
    assert not [e for e in cr.state.events if e["event_type"] == "change_request_cancelled"]


def test_race_the_checker_acted_meanwhile(cr, monkeypatch):
    declined(cr)
    real = agreements._decline_step

    def checker_acts_first(cursor, agreement_id):
        step = real(cursor, agreement_id)
        cr.state.vendors[7]["status"] = "01"  # the checker returned the application between our reads and our update
        cr.db.commit_now()
        return step

    monkeypatch.setattr(agreements, "_decline_step", checker_acts_first)
    r = cancel(cr)
    assert r.status_code == 409 and "just been handled" in r.get_json()["error"]
    assert cr.state.agreements[1]["status"] == "declined" and cr.state.vendors[7]["status"] == "01"
    assert not [e for e in cr.state.events if e["event_type"] == "change_request_cancelled"]


def test_only_vendors_with_a_change_request_can_cancel(cr):
    cr.state.vendors[7]["status"] = "agreement_sent"
    cr.db.commit_now()
    assert cancel(cr).status_code == 403
