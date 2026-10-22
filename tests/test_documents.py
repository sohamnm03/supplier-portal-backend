"""DELETE /vendors/me/documents: a vendor removes one uploaded supporting document."""
import pytest

import app as portal


class Blob:
    def __init__(self, name, doc_type, original="scan.pdf"):
        self.name, self.size = name, 10
        self.metadata = {"doc_type": doc_type, "original_name": original, "uploaded_at": "now"}


class Container:
    def __init__(self, *blobs):
        self.blobs = {b.name: b for b in blobs}
        self.deleted = []

    def list_blobs(self, name_starts_with="", include=None):
        return [b for n, b in self.blobs.items() if n.startswith(name_starts_with)]

    def delete_blob(self, name):
        self.deleted.append(name)
        self.blobs.pop(name)


@pytest.fixture
def docs(env, monkeypatch):
    env.state.vendors[7]["status"] = "registered"  # a draft
    env.db.commit_now()
    env.container = Container(
        Blob("vendor_7/pan_aaa111.pdf", "pan"), Blob("vendor_7/bank_bbb222.pdf", "bank", "bank.pdf"),
        Blob("vendor_7/agreement_ccc.pdf", "agreement"), Blob("vendor_8/pan_ddd444.pdf", "pan"),
    )
    monkeypatch.setattr(portal, "_docs_container", lambda: env.container)
    return env


def remove(env, **body):
    return env.client.delete("/vendors/me/documents", headers=env.headers, json=body)


def test_listing_identifies_each_file(docs):
    items = docs.client.get("/vendors/me/documents", headers=docs.headers).get_json()["documents"]
    assert sorted(i["blob_name"] for i in items) == ["vendor_7/bank_bbb222.pdf", "vendor_7/pan_aaa111.pdf"]


def test_removes_one_file_and_returns_the_rest(docs):
    r = remove(docs, doc_type="pan", blob_name="vendor_7/pan_aaa111.pdf")
    assert r.status_code == 200
    assert [i["blob_name"] for i in r.get_json()["documents"]] == ["vendor_7/bank_bbb222.pdf"]
    assert docs.container.deleted == ["vendor_7/pan_aaa111.pdf"]


def test_query_string_works_too(docs):
    r = docs.client.delete("/vendors/me/documents?doc_type=bank&blob_name=vendor_7/bank_bbb222.pdf", headers=docs.headers)
    assert r.status_code == 200 and docs.container.deleted == ["vendor_7/bank_bbb222.pdf"]


@pytest.mark.parametrize("blob", ["vendor_8/pan_ddd444.pdf", "vendor_7/pan_missing.pdf", "vendor_7/agreement_ccc.pdf", "vendor_7/../vendor_8/pan_ddd444.pdf", ""])
def test_not_found_or_not_yours(docs, blob):
    r = remove(docs, doc_type="pan", blob_name=blob)
    assert r.status_code == 404 and docs.container.deleted == []


def test_wrong_type_for_the_file_and_unknown_type(docs):
    assert remove(docs, doc_type="bank", blob_name="vendor_7/pan_aaa111.pdf").status_code == 404
    assert remove(docs, doc_type="agreement", blob_name="vendor_7/agreement_ccc.pdf").status_code == 400
    assert docs.container.deleted == []


def test_storage_not_configured(docs, monkeypatch):
    monkeypatch.setattr(portal, "_docs_container", lambda: None)
    r = remove(docs, doc_type="pan", blob_name="vendor_7/pan_aaa111.pdf")
    assert r.status_code == 503 and r.get_json()["error"] == "Document storage is not configured."


@pytest.mark.parametrize("status", ["01", "Pending", "active", "agreement_sent"])
def test_only_draft_and_returned_stages(docs, status):
    docs.state.vendors[7]["status"] = status
    docs.db.commit_now()
    r = remove(docs, doc_type="pan", blob_name="vendor_7/pan_aaa111.pdf")
    assert r.status_code == 403 and docs.container.deleted == []


def test_returned_vendor_can_remove(docs):
    docs.state.vendors[7]["status"] = "returned"
    docs.db.commit_now()
    assert remove(docs, doc_type="pan", blob_name="vendor_7/pan_aaa111.pdf").status_code == 200


def test_needs_a_token(docs):
    assert docs.client.delete("/vendors/me/documents", json={}).status_code == 401


# --- preview: GET /vendors/me/documents/content ---------------------------------------------------------------

class Download:
    def __init__(self, data):
        self.data = data

    def readall(self):
        return self.data


class Client:
    def __init__(self, data):
        self.data = data

    def download_blob(self):
        return Download(self.data)


def content(env, blob_name):
    return env.client.get("/vendors/me/documents/content", headers=env.headers, query_string={"blob_name": blob_name})


def test_content_streams_own_file_with_its_type(docs):
    docs.container.get_blob_client = lambda name: Client(b"%PDF-1.4 hello")
    r = content(docs, "vendor_7/pan_aaa111.pdf")
    assert r.status_code == 200 and r.data == b"%PDF-1.4 hello" and r.mimetype == "application/pdf"
    assert "no-store" in r.headers["Cache-Control"]


@pytest.mark.parametrize("blob", ["vendor_8/pan_ddd444.pdf", "vendor_7/agreement_ccc.pdf", "vendor_7/../vendor_8/pan_ddd444.pdf",
                                  "vendor_7/pan_missing.pdf", "vendor_70/pan_x.pdf", "", "../vendor_7/pan_aaa111.pdf"])
def test_content_other_vendor_agreement_traversal_missing_are_404(docs, blob):
    docs.container.get_blob_client = lambda name: pytest.fail("must not be read")
    assert content(docs, blob).status_code == 404


def test_content_storage_missing_and_token(docs, monkeypatch):
    monkeypatch.setattr(portal, "_docs_container", lambda: None)
    r = content(docs, "vendor_7/pan_aaa111.pdf")
    assert r.status_code == 503 and r.get_json()["error"] == "Document storage is not configured."
    assert docs.client.get("/vendors/me/documents/content?blob_name=vendor_7/pan_aaa111.pdf").status_code == 401


# --- upload: replace by default, ?append=1 for multi-file types ---------------------------------------------

import io


class UploadContainer(Container):
    def upload_blob(self, name, data, overwrite=False, content_settings=None, metadata=None):
        blob = Blob(name, (metadata or {}).get("doc_type"), (metadata or {}).get("original_name", "x.pdf"))
        self.blobs[name] = blob


def upload(env, doc_type, count, query=""):
    data = {doc_type: [(io.BytesIO(b"%PDF-1.4 x"), f"f{i}.pdf") for i in range(count)]}
    return env.client.post(f"/vendors/7/documents{query}", headers=env.headers, data=data, content_type="multipart/form-data")


@pytest.fixture
def up(docs, monkeypatch):
    docs.container = UploadContainer(Blob("vendor_7/pan_aaa111.pdf", "pan"), Blob("vendor_7/other_o1.pdf", "other"), Blob("vendor_7/other_o2.pdf", "other"))
    monkeypatch.setattr(portal, "_docs_container", lambda: docs.container)
    monkeypatch.setattr(portal.rejections, "mark_fixed", lambda *a, **k: None)

    class VendorCursor:
        def execute(self, sql, params=()):
            pass

        def fetchone(self):
            return {"vendor_id": 7, "status": "registered"}

        def close(self):
            pass

    class VendorConn:
        def cursor(self, dictionary=False):
            return VendorCursor()

        def close(self):
            pass

    monkeypatch.setattr(portal, "get_connection", lambda: VendorConn())
    return docs


def names(env, doc_type):
    return sorted(n for n in env.container.blobs if n.startswith(f"vendor_7/{doc_type}_"))


def test_upload_replaces_the_type_by_default(up):
    assert upload(up, "pan", 1).status_code == 201
    assert "vendor_7/pan_aaa111.pdf" not in names(up, "pan") and len(names(up, "pan")) == 1
    assert upload(up, "other", 1).status_code == 201
    assert len(names(up, "other")) == 1  # the two earlier files were replaced


def test_append_adds_up_to_the_limit(up):
    assert upload(up, "other", 1, "?append=1").status_code == 201  # 2 there + 1 = 3, the limit
    assert len(names(up, "other")) == 3
    r = upload(up, "other", 1, "?append=1")
    assert r.status_code == 400 and "at most 3" in r.get_json()["error"]
    assert len(names(up, "other")) == 3


def test_append_on_a_single_file_type_is_limited_to_one(up):
    assert upload(up, "pan", 1, "?append=1").status_code == 400
    assert names(up, "pan") == ["vendor_7/pan_aaa111.pdf"]
