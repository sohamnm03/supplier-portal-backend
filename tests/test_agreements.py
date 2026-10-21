from datetime import timedelta

import pytest

import agreements
import auth
from conftest import FakeResponse


# --- stage mapping ----------------------------------------------------------------------------------------

@pytest.mark.parametrize("status,stage", [
    ("agreement_sent", "agreement"), ("agreement_signed", "agreement_signed"), ("active", "active"),
    ("01", "checker_review"), ("Pending", "maker_review"), ("Rejected", "rejected"), ("returned", "returned"),
])
def test_stage_for(status, stage):
    assert auth.stage_for(status) == stage


def test_agreement_stages_closed_routes(env):
    r = env.client.put("/vendors/me/draft", headers=env.headers, json={})
    assert r.status_code == 403 and r.get_json()["code"] == "stage_not_allowed"
    assert env.client.post("/vendors/me/submit", headers=env.headers, json={}).status_code == 403
    assert env.client.post("/vendors/me/change-request", headers=env.headers, json={"fields": ["pan"]}).status_code == 403


def test_agreement_routes_need_token(env):
    assert env.client.get("/vendors/me/agreements").status_code == 401


def test_other_stage_cannot_use_agreements(env):
    env.state.vendors[7]["status"] = "Rejected"
    assert env.client.get("/vendors/me/agreements", headers=env.headers).status_code == 403
    # the history stays readable while the application is with the reviewers again, but nothing can be signed from there
    env.state.vendors[7]["status"] = "01"
    env.db.commit_now()
    assert env.client.get("/vendors/me/agreements", headers=env.headers).status_code == 200
    assert env.client.post("/vendors/me/agreements/1/send-code", headers=env.headers).status_code == 403


# --- list --------------------------------------------------------------------------------------------------

def test_list_states_and_order(env):
    s = env.state
    s.vendors[7]["status"] = "active"
    s.add_agreement(1, 1, "signed", end="2026-09-30", signed_pdf_path="p1")   # expired, older
    s.add_agreement(2, 2, "signed", end="2027-09-30", signed_pdf_path="p2")   # replaced by v3
    s.add_agreement(3, 3, "signed", end="2028-09-30", signed_pdf_path="p3")   # in force
    s.add_agreement(4, 4, "sent")                                              # renewal awaiting
    s.add_agreement(5, 5, "declined")
    s.add_agreement(6, 6, "withdrawn")
    env.db.commit_now()
    items = env.client.get("/vendors/me/agreements", headers=env.headers).get_json()
    assert [i["agreement_id"] for i in items] == [5, 4, 3, 2, 1]  # newest first; the declined one is kept for the record, a withdrawn one is not
    declined = next(i for i in items if i["agreement_id"] == 5)
    assert declined["derived_state"] == "declined" and not declined["can_sign"] and not declined["is_current"] and not declined["has_signed_pdf"]
    by_id = {i["agreement_id"]: i for i in items}
    assert by_id[4]["derived_state"] == "awaiting" and by_id[4]["can_sign"] and not by_id[4]["is_current"]
    assert by_id[3]["derived_state"] == "active" and by_id[3]["is_current"] and not by_id[3]["can_sign"]
    assert by_id[2]["derived_state"] == "replaced" and not by_id[2]["is_current"]
    assert by_id[1]["derived_state"] == "replaced"  # a newer signed version exists
    assert by_id[3]["has_signed_pdf"] and not by_id[4]["has_signed_pdf"]


def test_list_expired_when_nothing_newer(env):
    env.state.vendors[7]["status"] = "active"
    env.state.add_agreement(1, 1, "signed", end="2026-09-30", signed_pdf_path="p1")
    env.db.commit_now()
    item = env.client.get("/vendors/me/agreements", headers=env.headers).get_json()[0]
    assert item["derived_state"] == "expired" and not item["is_current"]


def test_link_expired_cannot_sign(env):
    env.state.add_agreement(1, 1, "sent", expires_in_days=-1)
    env.db.commit_now()
    item = env.client.get("/vendors/me/agreements", headers=env.headers).get_json()[0]
    assert item["derived_state"] == "awaiting" and item["link_expired"] and not item["can_sign"]


def test_list_for_signed_stage_is_read_only(env):
    env.state.vendors[7]["status"] = "agreement_signed"
    env.state.add_agreement(1, 1, "signed", signed_pdf_path="p1")
    env.db.commit_now()
    r = env.client.get("/vendors/me/agreements", headers=env.headers)
    assert r.status_code == 200 and r.get_json()[0]["derived_state"] == "active"


def test_me_includes_open_agreement(env, monkeypatch):
    env.state.add_agreement(1, 1, "sent")
    env.db.commit_now()
    summary = agreements.open_agreement_summary(7, "agreement")
    assert summary["agreement_id"] == 1 and summary["can_sign"]
    env.state.agreements[1]["status"] = "signed"
    assert agreements.open_agreement_summary(7, "agreement_signed") is None


# --- pdf ---------------------------------------------------------------------------------------------------

def test_pdf_first_view_and_download(env):
    env.state.add_agreement(1, 1, "sent")
    env.container.blobs["vendor_7/AGR-2026-00001.pdf"] = b"%PDF-unsigned"
    env.db.commit_now()
    r = env.client.get("/vendors/me/agreements/1/pdf", headers=env.headers)
    assert r.status_code == 200 and r.data == b"%PDF-unsigned" and r.mimetype == "application/pdf"
    assert "inline" in r.headers["Content-Disposition"]
    assert [e["event_type"] for e in env.state.events] == ["viewed"]
    env.client.get("/vendors/me/agreements/1/pdf", headers=env.headers)
    assert [e["event_type"] for e in env.state.events] == ["viewed"]  # first open only
    r = env.client.get("/vendors/me/agreements/1/pdf?download=1", headers=env.headers)
    assert "attachment" in r.headers["Content-Disposition"]
    assert [e["event_type"] for e in env.state.events] == ["viewed", "downloaded"]


def test_pdf_other_vendors_agreement_404(env):
    env.state.vendors[8] = {"vendor_id": 8, "email": "o@x.test", "status": "agreement_sent", "submission_round": 1}
    env.state.add_agreement(1, 1, "sent", vendor_id=8)
    env.db.commit_now()
    assert env.client.get("/vendors/me/agreements/1/pdf", headers=env.headers).status_code == 404


def test_pdf_declined_hidden_and_signed_copy_served(env):
    env.state.add_agreement(1, 1, "declined")
    env.state.add_agreement(2, 2, "signed", signed_pdf_path="vendor_7/AGR-2026-00002_signed.pdf")
    env.container.blobs["vendor_7/AGR-2026-00002_signed.pdf"] = b"%PDF-signed"
    env.db.commit_now()
    env.container.blobs["vendor_7/AGR-2026-00001.pdf"] = b"%PDF-unsigned"
    assert env.client.get("/vendors/me/agreements/1/pdf", headers=env.headers).data == b"%PDF-unsigned"  # a declined one is kept as issued
    assert env.client.get("/vendors/me/agreements/2/pdf", headers=env.headers).data == b"%PDF-signed"


# --- codes -------------------------------------------------------------------------------------------------

def test_send_code_stores_verification_id_and_rate_limits(env):
    env.state.add_agreement(1, 1, "sent")
    env.db.commit_now()
    r = env.client.post("/vendors/me/agreements/1/send-code", headers=env.headers)
    assert r.status_code == 200 and "v***@vendor.test" in r.get_json()["sent_to"]
    assert env.verification_calls[0][2]["json"] == {"email": "v@vendor.test"}
    assert env.state.otps[0]["code_hash"] == "vid-1" and env.state.otps[0]["sent_to"] == "v@vendor.test"
    assert env.state.events[-1]["event_type"] == "code_sent"
    again = env.client.post("/vendors/me/agreements/1/send-code", headers=env.headers)
    assert again.status_code == 429 and again.headers["Retry-After"]
    assert len(env.state.otps) == 1
    env.state.now += timedelta(seconds=61)
    env.db.commit_now()
    assert env.client.post("/vendors/me/agreements/1/send-code", headers=env.headers).status_code == 200


def test_verify_code_attempt_limit(env):
    env.state.add_agreement(1, 1, "sent")
    env.db.commit_now()
    env.send_code()
    env.verify_answer = FakeResponse({"success": False, "message": "Incorrect code"}, 400)
    for n in range(5):
        r = env.client.post("/vendors/me/agreements/1/verify-code", headers=env.headers, json={"otp": "000000"})
        assert r.status_code == 400 and r.get_json()["attempts_left"] == 4 - n
    calls = len(env.verification_calls)
    r = env.client.post("/vendors/me/agreements/1/verify-code", headers=env.headers, json={"otp": "123456"})
    assert r.status_code == 429
    assert len(env.verification_calls) == calls  # the service is not asked a sixth time
    assert env.state.otps[0]["attempts"] == 5


def test_verify_code_success_logs_event(env):
    env.state.add_agreement(1, 1, "sent")
    env.db.commit_now()
    env.send_code()
    r = env.client.post("/vendors/me/agreements/1/verify-code", headers=env.headers, json={"otp": "123456"})
    assert r.status_code == 200 and r.get_json()["verified"] is True
    assert env.state.events[-1]["event_type"] == "code_verified"
    assert env.client.post("/vendors/me/agreements/1/verify-code", headers=env.headers, json={}).status_code == 400


def test_verify_code_without_code_requested(env):
    env.state.add_agreement(1, 1, "sent")
    env.db.commit_now()
    assert env.client.post("/vendors/me/agreements/1/verify-code", headers=env.headers, json={"otp": "123456"}).status_code == 400


# --- sign --------------------------------------------------------------------------------------------------

def prepared_onboarding(env):
    env.state.add_agreement(1, 1, "sent")
    env.db.commit_now()
    env.send_code(1)


def test_sign_onboarding_happy_path(env, monkeypatch):
    monkeypatch.setenv("VENDOR_AGREEMENT_SIGNED_TEMPLATE", "t_signed")
    monkeypatch.setenv("VENDOR_AGREEMENT_SIGNED_NOTICE_TEMPLATE", "t_notice")
    prepared_onboarding(env)
    r = env.client.post("/vendors/me/agreements/1/sign", headers=env.headers, json=env.sign_body(
        signer_name="  Vee   Vendor ", agreement_id=2, status="x", signed_pdf_path="evil"))
    assert r.status_code == 200, r.get_json()
    assert r.get_json()["stage"] == "agreement_signed"
    row = env.state.agreements[1]
    assert row["status"] == "signed" and row["signer_name"] == "Vee Vendor" and row["signature_method"] == "draw"
    assert row["signed_pdf_path"] == "vendor_7/AGR-2026-00001_signed.pdf"
    assert row["vendor_id"] == 7
    assert env.state.vendors[7]["status"] == "agreement_signed"
    assert env.container.uploads == [row["signed_pdf_path"]]  # uploaded once
    pdf = env.container.blobs[row["signed_pdf_path"]]
    assert pdf.startswith(b"%PDF") and row["signed_pdf_sha256"] == __import__("hashlib").sha256(pdf).hexdigest()
    assert env.state.otps[0]["used_at"] is not None
    assert [e["event_type"] for e in env.state.events] == ["code_sent", "details_confirmed", "signed"]
    # emails: vendor confirmation + notice to each checker; declined template unset so nothing for it
    assert sorted(m[0] for m in env.emails) == ["checker1@acme.test", "checker2@acme.test", "v@vendor.test"]
    assert {m[2] for m in env.emails} == {"t_signed", "t_notice"}
    # a second attempt cannot sign again
    again = env.client.post("/vendors/me/agreements/1/sign", headers=env.headers, json=env.sign_body())
    assert again.status_code == 409 and "already" in again.get_json()["error"]
    assert len(env.container.uploads) == 1


def test_sign_emails_skipped_when_templates_empty(env):
    prepared_onboarding(env)
    assert env.client.post("/vendors/me/agreements/1/sign", headers=env.headers, json=env.sign_body()).status_code == 200
    assert env.emails == []


def test_sign_renewal_leaves_vendor_status(env):
    env.state.vendors[7]["status"] = "active"
    env.state.add_agreement(1, 1, "signed", signed_pdf_path="old", end="2026-12-31")
    env.state.add_agreement(2, 2, "sent")
    env.db.commit_now()
    env.send_code(2)
    r = env.client.post("/vendors/me/agreements/2/sign", headers=env.headers, json=env.sign_body())
    assert r.status_code == 200 and r.get_json()["stage"] == "active"
    assert env.state.vendors[7]["status"] == "active"
    assert env.state.agreements[2]["status"] == "signed"
    items = env.client.get("/vendors/me/agreements", headers=env.headers).get_json()
    assert [(i["agreement_id"], i["derived_state"]) for i in items] == [(2, "active"), (1, "replaced")]


def test_sign_refusals(env):
    prepared_onboarding(env)
    post = lambda **over: env.client.post("/vendors/me/agreements/1/sign", headers=env.headers, json=env.sign_body(**over))
    assert post(details_confirmed=False).status_code == 400
    assert post(details_confirmed="true").status_code == 400
    assert post(signer_name="A").status_code == 400
    assert post(signer_name="x" * 151).status_code == 400
    assert post(signer_designation=" ").status_code == 400
    assert post(method="scribble").status_code == 400
    assert post(signature="data:image/png;base64,AAAA").status_code == 400
    assert post(signature="not a data url").status_code == 400
    assert post(signature="").status_code == 400
    assert env.container.uploads == [] and env.state.agreements[1]["status"] == "sent"
    assert env.state.vendors[7]["status"] == "agreement_sent"


def test_sign_blank_signature_refused(env):
    import base64, io
    from PIL import Image
    prepared_onboarding(env)
    out = io.BytesIO()
    Image.new("RGBA", (50, 20), (0, 0, 0, 0)).save(out, format="PNG")
    blank = "data:image/png;base64," + base64.b64encode(out.getvalue()).decode()
    r = env.client.post("/vendors/me/agreements/1/sign", headers=env.headers, json=env.sign_body(signature=blank))
    assert r.status_code == 400


def test_sign_requires_verified_code(env):
    env.state.add_agreement(1, 1, "sent")
    env.db.commit_now()
    body = env.sign_body()
    # no code requested at all
    assert env.client.post("/vendors/me/agreements/1/sign", headers=env.headers, json=body).status_code == 403
    env.send_code(1)
    env.verified_error = ("Verify your email address before submitting.", 403)  # service says not verified
    r = env.client.post("/vendors/me/agreements/1/sign", headers=env.headers, json=body)
    assert r.status_code == 403 and r.get_json()["code"] == "otp_required"
    assert env.state.agreements[1]["status"] == "sent" and env.container.uploads == []
    # a code that expired
    env.verified_error = None
    env.state.now += timedelta(minutes=11)
    env.db.commit_now()
    assert env.client.post("/vendors/me/agreements/1/sign", headers=env.headers, json=body).status_code == 403


def test_sign_code_sent_to_another_email_refused(env):
    prepared_onboarding(env)
    env.state.otps[0]["sent_to"] = "someone@else.test"
    env.db.commit_now()
    assert env.client.post("/vendors/me/agreements/1/sign", headers=env.headers, json=env.sign_body()).status_code == 403


def test_sign_wrong_vendor_404(env):
    env.state.vendors[8] = {"vendor_id": 8, "email": "o@x.test", "status": "agreement_sent", "submission_round": 1}
    env.state.add_agreement(1, 1, "sent", vendor_id=8)
    env.db.commit_now()
    assert env.client.post("/vendors/me/agreements/1/sign", headers=env.headers, json=env.sign_body()).status_code == 404
    assert env.client.post("/vendors/me/agreements/1/decline", headers=env.headers, json={"reason": "no way"}).status_code == 404
    assert env.client.post("/vendors/me/agreements/1/send-code", headers=env.headers).status_code == 404


def test_sign_withdrawn_is_409(env):
    prepared_onboarding(env)
    env.state.agreements[1]["status"] = "withdrawn"
    env.db.commit_now()
    r = env.client.post("/vendors/me/agreements/1/sign", headers=env.headers, json=env.sign_body())
    assert r.status_code == 409 and "withdrawn" in r.get_json()["error"]
    assert env.container.uploads == []


def test_sign_withdrawn_during_signing_discards_upload(env, monkeypatch):
    """Withdrawn by the checker between the check and the conditional UPDATE: 409, nothing saved, blob removed."""
    prepared_onboarding(env)
    real = env.container.upload_blob

    def upload_then_withdraw(name, data, **kw):
        real(name, data, **kw)
        env.state.agreements[1]["status"] = "withdrawn"
        env.db.commit_now()

    env.container.upload_blob = upload_then_withdraw
    r = env.client.post("/vendors/me/agreements/1/sign", headers=env.headers, json=env.sign_body())
    assert r.status_code == 409 and "withdrawn" in r.get_json()["error"]
    assert env.state.vendors[7]["status"] == "agreement_sent"
    assert env.container.deleted == ["vendor_7/AGR-2026-00001_signed.pdf"]


def test_sign_expired_is_410(env):
    env.state.add_agreement(1, 1, "sent", expires_in_days=-1)
    env.db.commit_now()
    r = env.client.post("/vendors/me/agreements/1/sign", headers=env.headers, json=env.sign_body())
    assert r.status_code == 410 and "new one" in r.get_json()["error"]
    assert env.client.post("/vendors/me/agreements/1/send-code", headers=env.headers).status_code == 410


def test_sign_vendor_moved_on_rolls_back(env):
    prepared_onboarding(env)
    # The vendor's status is no longer agreement_sent when the transaction runs (the checker acted at the same
    # moment): the conditional vendor UPDATE matches nothing, so the agreement UPDATE is rolled back with it.
    original = agreements._kind
    agreements._kind = lambda status: "onboarding"
    try:
        env.state.vendors[7]["status"] = "active"
        env.db.commit_now()
        r = env.client.post("/vendors/me/agreements/1/sign", headers=env.headers, json=env.sign_body())
    finally:
        agreements._kind = original
    assert r.status_code == 409
    assert env.state.agreements[1]["status"] == "sent"  # rolled back together
    assert env.state.otps[0]["used_at"] is None


def test_sign_without_storage_is_503(env, monkeypatch):
    prepared_onboarding(env)
    monkeypatch.setitem(agreements.deps, "docs_container", lambda: None)
    r = env.client.post("/vendors/me/agreements/1/sign", headers=env.headers, json=env.sign_body())
    assert r.status_code == 503 and env.state.agreements[1]["status"] == "sent"


# --- decline -----------------------------------------------------------------------------------------------

def test_decline_onboarding_restores_01(env, monkeypatch):
    monkeypatch.setenv("VENDOR_AGREEMENT_DECLINED_TEMPLATE", "t_declined")
    env.state.add_agreement(1, 1, "sent")
    env.db.commit_now()
    assert env.client.post("/vendors/me/agreements/1/decline", headers=env.headers, json={"reason": "no"}).status_code == 400
    r = env.client.post("/vendors/me/agreements/1/decline", headers=env.headers, json={"reason": "Terms not acceptable"})
    assert r.status_code == 200
    row = env.state.agreements[1]
    assert row["status"] == "declined" and row["decline_reason"] == "Terms not acceptable"
    assert env.state.vendors[7]["status"] == "01"
    assert env.state.events[-1]["event_type"] == "declined" and env.state.events[-1]["detail"] == {"reason": "Terms not acceptable", "at_step": "sign"}
    assert sorted(m[0] for m in env.emails) == ["checker1@acme.test", "checker2@acme.test"]
    assert env.emails[0][3]["reason"] == "Terms not acceptable"
    # the declined agreement stays in the vendor's history, with the reason and where it was declined
    items = env.client.get("/vendors/me/agreements", headers=env.headers).get_json()  # now in checker_review
    assert [(i["agreement_id"], i["derived_state"], i["decline_reason"], i["decline_step"]) for i in items] == [(1, "declined", "Terms not acceptable", "sign")]


def test_decline_renewal_changes_nothing_else(env):
    env.state.vendors[7]["status"] = "active"
    env.state.add_agreement(2, 2, "sent")
    env.db.commit_now()
    assert env.client.post("/vendors/me/agreements/2/decline", headers=env.headers, json={"reason": "later please"}).status_code == 200
    assert env.state.vendors[7]["status"] == "active" and env.state.agreements[2]["status"] == "declined"


def test_decline_too_long_and_withdrawn(env):
    env.state.add_agreement(1, 1, "sent")
    env.db.commit_now()
    assert env.client.post("/vendors/me/agreements/1/decline", headers=env.headers, json={"reason": "x" * 501}).status_code == 400
    env.state.agreements[1]["status"] = "withdrawn"
    env.db.commit_now()
    assert env.client.post("/vendors/me/agreements/1/decline", headers=env.headers, json={"reason": "fine"}).status_code == 409


# --- public verification -----------------------------------------------------------------------------------

def test_public_verify(env):
    env.state.add_agreement(1, 1, "signed", signed_pdf_path="p", signed_at=env.state.now)
    env.state.add_agreement(2, 2, "sent")
    env.db.commit_now()
    sha = env.state.agreements[1]["unsigned_pdf_sha256"]
    r = env.client.get(f"/agreements/verify?no=AGR-2026-00001&h={sha[:12]}")  # no token needed
    assert r.get_json() == {"valid": True, "agreement_no": "AGR-2026-00001", "vendor_name": "Vendor Pvt Ltd",
                            "signed_at": env.state.now.isoformat()}
    for query in ["no=AGR-2026-00001&h=000000000000", "no=AGR-2026-00009&h=" + sha[:12], "no=AGR-2026-00001",
                  "no=AGR-2026-00001&h=" + sha[:5], "h=" + sha[:12], "no=AGR-2026-00001&h=" + sha[:12] + "ff",
                  "no=AGR-2026-00002&h=" + env.state.agreements[2]["unsigned_pdf_sha256"][:12]]:  # sent, not signed
        assert env.client.get("/agreements/verify?" + query).get_json() == {"valid": False}, query


def test_list_has_key_terms(env):
    import json
    row = env.state.add_agreement(1, 1, "sent")
    terms = json.loads(row["terms_json"])
    terms["terms"] = {"term_months": 12, "payment_days": 45, "notice_days": 30, "arbitration_city": "Mumbai"}
    row["terms_json"] = json.dumps(terms)
    env.state.add_agreement(2, 2, "sent")  # no terms stored at all
    env.db.commit_now()
    items = {i["agreement_id"]: i for i in env.client.get("/vendors/me/agreements", headers=env.headers).get_json()}
    assert items[1]["key_terms"] == {"term_months": 12, "payment_days": 45, "notice_days": 30}
    assert items[2]["key_terms"] == {}
    assert items[1]["company_name"] == "Acme Ltd"
    assert items[1]["company_signatory_name"] == "A Signatory" and items[1]["company_signatory_designation"] == "Director"


def _states(env):
    items = env.client.get("/vendors/me/agreements", headers=env.headers).get_json()
    return {i["agreement_id"]: (i["derived_state"], i["is_current"]) for i in items}


def test_scheduled_renewal_does_not_replace_current(env):
    s = env.state  # today is 2026-10-20
    s.vendors[7]["status"] = "active"
    s.add_agreement(1, 1, "signed", end="2027-09-30", signed_pdf_path="p1")
    row = s.add_agreement(2, 2, "signed", end="2028-09-30", signed_pdf_path="p2")
    import json
    terms = json.loads(row["terms_json"])
    terms["start_iso"] = "2027-10-01"
    row["terms_json"] = json.dumps(terms)
    env.db.commit_now()
    assert _states(env) == {2: ("scheduled", False), 1: ("active", True)}


def test_scheduled_becomes_active_then_replaces(env):
    import json
    s = env.state
    s.vendors[7]["status"] = "active"
    s.add_agreement(1, 1, "signed", end="2027-09-30", signed_pdf_path="p1")
    row = s.add_agreement(2, 2, "signed", end="2028-09-30", signed_pdf_path="p2")
    terms = json.loads(row["terms_json"])
    terms["start_iso"] = "2026-10-20"  # starts today
    row["terms_json"] = json.dumps(terms)
    env.db.commit_now()
    assert _states(env) == {2: ("active", True), 1: ("replaced", False)}


def test_only_signed_agreement_scheduled_and_expired_still_expired(env):
    import json
    s = env.state
    s.vendors[7]["status"] = "active"
    s.add_agreement(1, 1, "signed", end="2026-09-30", signed_pdf_path="p1")  # ended, nothing newer started
    row = s.add_agreement(2, 2, "signed", end="2028-09-30", signed_pdf_path="p2")
    terms = json.loads(row["terms_json"])
    terms["start_iso"] = "2027-01-01"
    row["terms_json"] = json.dumps(terms)
    env.db.commit_now()
    assert _states(env) == {2: ("scheduled", False), 1: ("expired", False)}


# --- decline step, update_info, declined_agreement ----------------------------------------------------------

def _set_terms(row, **changes):
    import json
    terms = json.loads(row["terms_json"])
    for key, value in changes.items():
        section, name = key.split("__")
        terms.setdefault(section, {})[name] = value
    if "note" in changes:
        pass
    row["terms_json"] = json.dumps(terms)


def test_decline_stores_step_and_rejects_bad_step(env):
    env.state.add_agreement(1, 1, "sent")
    env.db.commit_now()
    bad = env.client.post("/vendors/me/agreements/1/decline", headers=env.headers, json={"reason": "wrong data", "at_step": "x"})
    assert bad.status_code == 400
    ok = env.client.post("/vendors/me/agreements/1/decline", headers=env.headers, json={"reason": "wrong data", "at_step": "sign"})
    assert ok.status_code == 200 and ok.get_json()["stage"] == "checker_review"
    assert env.state.events[-1]["detail"] == {"reason": "wrong data", "at_step": "sign"}
    assert env.state.vendors[7]["status"] == "01"


def test_decline_at_review_is_a_change_request(env):
    env.state.vendors[7]["status"] = "agreement_sent"; env.state.events.clear(); env.db.commit_now()
    env.state.add_agreement(1, 1, "sent")
    env.db.commit_now()
    post = lambda **body: env.client.post("/vendors/me/agreements/1/decline", headers=env.headers, json={"reason": "wrong data", "at_step": "review", **body})
    assert post().status_code == 400                          # the details to change are required
    assert post(fields=["nope"]).status_code == 400           # and must be changeable ones
    assert env.state.agreements[1]["status"] == "sent" and env.state.vendors[7]["status"] == "agreement_sent"
    assert post(reason="", fields=["pan"]).status_code == 200 and env.state.agreements[1]["decline_reason"] == "Details need to be corrected."
    env.state.agreements[1].update(status="sent", declined_at=None, decline_reason=None); env.state.vendors[7]["status"] = "agreement_sent"; env.state.events.clear(); env.db.commit_now()
    ok = post(fields=["pan", "ifsc", "pan"])
    assert ok.status_code == 200 and ok.get_json()["stage"] == "change_requested"
    assert env.state.vendors[7]["status"] == "change_req_checker" and env.state.vendors[7]["correction_fields"] == "pan,ifsc"
    assert env.state.agreements[1]["status"] == "declined"
    assert env.state.events[-1]["detail"] == {"reason": "wrong data", "at_step": "review", "fields": ["pan", "ifsc"]}
    assert env.change_emails[-1] == ("change_req_checker", ["PAN", "IFSC"])


def test_update_info_with_and_without_detail_changes(env):
    import json
    s = env.state
    s.add_agreement(1, 1, "declined", decline_reason="PAN is wrong")
    s.events.append({"agreement_id": 1, "event_type": "declined", "actor": "v", "detail": {"reason": "PAN is wrong", "at_step": "review"}, "ip": None})
    v2 = s.add_agreement(2, 2, "sent")
    _set_terms(v2, vendor__pan="ZZZZZ9999Z", terms__payment_days=30)
    terms = json.loads(v2["terms_json"])
    terms["issue_note"] = "Fixed your PAN"
    v2["terms_json"] = json.dumps(terms)
    env.db.commit_now()
    items = env.client.get("/vendors/me/agreements", headers=env.headers).get_json()
    info = items[0]["update_info"]
    assert info["previous_version"] == 1 and info["previous_decline_reason"] == "PAN is wrong"
    assert info["previous_decline_step"] == "review" and info["issue_note"] == "Fixed your PAN"
    assert info["details_changed"] is True
    labels = {c["label"]: (c["from"], c["to"]) for c in info["changes"]}
    assert labels["PAN"] == ("ABCDE1234F", "ZZZZZ9999Z") and labels["Payment within (days)"] == ("", "30")

    # only a term changed: details_changed stays false; version 1 items have no update_info
    s.agreements[2]["terms_json"] = json.dumps({**json.loads(s.agreements[1]["terms_json"]), "terms": {"payment_days": 45}})
    env.db.commit_now()
    info = env.client.get("/vendors/me/agreements", headers=env.headers).get_json()[0]["update_info"]
    assert info["details_changed"] is False and [c["label"] for c in info["changes"]] == ["Payment within (days)"]


def test_update_info_none_for_v1_and_empty_changes(env):
    import json
    s = env.state
    s.add_agreement(1, 1, "withdrawn")
    v2 = s.add_agreement(2, 2, "sent")
    v2["terms_json"] = s.agreements[1]["terms_json"]
    s.add_agreement(3, 3, "sent")  # not allowed in practice, but v1 below must stay None
    s.agreements[3]["terms_json"] = s.agreements[1]["terms_json"]
    env.db.commit_now()
    items = {i["agreement_id"]: i for i in env.client.get("/vendors/me/agreements", headers=env.headers).get_json()}
    assert items[2]["update_info"]["changes"] == [] and items[2]["update_info"]["details_changed"] is False
    assert items[2]["update_info"]["previous_decline_step"] == ""
    s2 = json.loads(json.dumps(items[2]))  # plain data
    assert s2["update_info"]["issue_note"] == ""


def test_declined_agreement_summary(env):
    s = env.state
    s.add_agreement(1, 1, "declined", decline_reason="Wrong address", declined_at=s.now)
    s.events.append({"agreement_id": 1, "event_type": "declined", "actor": "v", "detail": {"reason": "Wrong address", "at_step": "review"}, "ip": None})
    env.db.commit_now()
    summary = agreements.declined_agreement_summary(7)
    assert summary["agreement_no"] == "AGR-2026-00001" and summary["reason"] == "Wrong address" and summary["at_step"] == "review"
    s.add_agreement(2, 2, "sent")  # a newer agreement: no longer "declined"
    env.db.commit_now()
    assert agreements.declined_agreement_summary(7) is None


def _with_snapshot(row, **over):
    import json
    terms = json.loads(row["terms_json"])
    snap = {"legal_name": "Vendor Pvt Ltd", "vendor_type": "Private Limited", "address": "2 Side Street", "pan": "ABCDE1234F",
            "gstin": "27ABCDE1234F1Z5", "email": "v@vendor.test", "phone": "+919999999999", "registration_number": "R1",
            "msme_status": "Not registered", "bank_holder": "Vendor Pvt Ltd", "bank_name": "HDFC", "bank_branch": "Fort",
            "bank_ifsc": "HDFC0001234", "bank_account_masked": "••••1234"}
    snap.update(over)
    terms["details_snapshot"] = snap
    row["terms_json"] = json.dumps(terms)


def test_update_info_bank_name_only_change(env):
    s = env.state
    _with_snapshot(s.add_agreement(1, 1, "declined"))
    _with_snapshot(s.add_agreement(2, 2, "sent"), bank_name="ICICI", bank_ifsc="ICIC0000001")
    env.db.commit_now()
    info = env.client.get("/vendors/me/agreements", headers=env.headers).get_json()[0]["update_info"]
    assert info["snapshot_missing"] is False and info["details_changed"] is True and info["terms_changed"] is False
    assert info["changed_keys"] == ["bank_name", "bank_ifsc"]
    assert info["changes"][0] == {"key": "bank_name", "label": "Bank name", "group": "Bank", "from": "HDFC", "to": "ICICI"}


def test_update_info_snapshot_no_change_and_terms_only(env):
    s = env.state
    _with_snapshot(s.add_agreement(1, 1, "withdrawn"))
    v2 = s.add_agreement(2, 2, "sent")
    _with_snapshot(v2)
    env.db.commit_now()
    info = env.client.get("/vendors/me/agreements", headers=env.headers).get_json()[0]["update_info"]
    assert info["changes"] == [] and info["changed_keys"] == [] and not info["details_changed"] and not info["terms_changed"]
    _set_terms(env.state.agreements[2], terms__payment_days=45)
    env.db.commit_now()
    info = env.client.get("/vendors/me/agreements", headers=env.headers).get_json()[0]["update_info"]
    assert info["terms_changed"] is True and info["details_changed"] is False and info["changed_keys"] == []
    assert info["changes"][0]["group"] == "Terms"


def test_update_info_missing_old_snapshot_falls_back(env):
    s = env.state
    s.add_agreement(1, 1, "declined")  # legacy: no snapshot
    v2 = s.add_agreement(2, 2, "sent")
    _with_snapshot(v2, pan="ZZZZZ9999Z", bank_name="ICICI")
    import json
    terms = json.loads(v2["terms_json"])
    terms["vendor"]["pan"] = "ZZZZZ9999Z"
    v2["terms_json"] = json.dumps(terms)
    env.db.commit_now()
    info = env.client.get("/vendors/me/agreements", headers=env.headers).get_json()[0]["update_info"]
    assert info["snapshot_missing"] is True
    assert info["changed_keys"] == ["pan"]  # bank name could not be compared


# --- live preview -------------------------------------------------------------------------------------------

def test_preview_builds_pdf_and_records_nothing(env):
    env.state.add_agreement(1, 1, "sent")
    env.db.commit_now()
    body = env.sign_body(final=False)
    r = env.client.post("/vendors/me/agreements/1/preview", headers=env.headers, json=body)
    assert r.status_code == 200 and r.mimetype == "application/pdf" and r.data.startswith(b"%PDF")
    final = env.client.post("/vendors/me/agreements/1/preview", headers=env.headers, json={**body, "final": True})
    assert final.status_code == 200 and final.data.startswith(b"%PDF")
    # nothing is saved: no upload, still open, no events, no code used
    assert env.container.uploads == [] and env.state.agreements[1]["status"] == "sent" and env.state.events == []
    # a name alone (no signature yet) is fine too
    assert env.client.post("/vendors/me/agreements/1/preview", headers=env.headers, json={"signer_name": "Vee"}).status_code == 200


def test_preview_refusals(env):
    env.state.add_agreement(1, 1, "sent")
    env.state.add_agreement(2, 2, "signed", signed_pdf_path="p")
    env.db.commit_now()
    post = lambda agreement_id, **body: env.client.post(f"/vendors/me/agreements/{agreement_id}/preview", headers=env.headers, json=body)
    assert post(1, signature="not a data url").status_code == 400
    assert post(2).status_code == 404  # only an open agreement can be previewed
    assert post(99).status_code == 404
    assert env.client.post("/vendors/me/agreements/1/preview", json={}).status_code == 401
