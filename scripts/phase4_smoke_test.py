#!/usr/bin/env python3
"""Self-contained Phase 4 catalogue/intelligence smoke test using temporary SQLite."""
from __future__ import annotations

import os
import re
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
TMP = tempfile.TemporaryDirectory(prefix="controls-exchange-phase4-")
os.environ["DATABASE_PATH"] = str(Path(TMP.name) / "phase4.db")
os.environ["SEED_DEMO_DATA"] = "true"
os.environ["SEED_ADMIN"] = "true"
os.environ["EMAIL_PROVIDER"] = "log"
os.environ["ENVIRONMENT"] = "development"
os.environ.pop("OPENAI_API_KEY", None)

from fastapi.testclient import TestClient  # noqa: E402
import app  # noqa: E402
from catalog_intelligence import catalog_candidates, link_inventory_ids, source_assistant  # noqa: E402
from platform_core import db  # noqa: E402


def csrf(client: TestClient, path: str) -> str:
    response = client.get(path)
    assert response.status_code == 200, (path, response.status_code, response.text[:500])
    match = re.search(r'name="csrf_token" value="([^"]+)"', response.text)
    assert match, f"No CSRF token on {path}"
    return match.group(1)


def login(client: TestClient, email: str, password: str) -> None:
    response = client.post("/login", data={"csrf_token": csrf(client, "/login"), "email": email, "password": password, "next": "/dashboard"}, follow_redirects=False)
    assert response.status_code == 303, response.text


admin = TestClient(app.app)
with admin:
    login(admin, "admin@controlsexchange.local", "ChangeMe123!")
    assert admin.get("/admin/catalog").status_code == 200

    # Add two explicit test catalogue parts.
    token = csrf(admin, "/admin/catalog")
    r = admin.post("/admin/catalog/parts", data={
        "csrf_token": token, "manufacturer": "Test Controls", "part_number": "OLD-100",
        "name": "Legacy test controller", "product_family": "Test Family", "lifecycle_status": "obsolete",
        "description": "Phase 4 legacy test part", "source": "phase4 smoke test", "verified": "1",
    }, follow_redirects=False)
    assert r.status_code == 303
    r = admin.post("/admin/catalog/parts", data={
        "csrf_token": csrf(admin, "/admin/catalog"), "manufacturer": "Test Controls", "part_number": "NEW-200",
        "name": "Replacement test controller", "product_family": "Test Family", "lifecycle_status": "current",
        "description": "Phase 4 replacement test part", "source": "phase4 smoke test", "verified": "1",
    }, follow_redirects=False)
    assert r.status_code == 303
    with db() as conn:
        old_id = conn.execute("SELECT id FROM catalog_parts WHERE manufacturer='Test Controls' AND part_number='OLD-100'").fetchone()["id"]
        new_id = conn.execute("SELECT id FROM catalog_parts WHERE manufacturer='Test Controls' AND part_number='NEW-200'").fetchone()["id"]

    # Alias + reviewed supersession relationship.
    r = admin.post(f"/admin/catalog/{old_id}/aliases", data={
        "csrf_token": csrf(admin, f"/catalog/{old_id}"), "alias": "OLD100-A", "source": "phase4 smoke test", "verified": "1",
    }, follow_redirects=False)
    assert r.status_code == 303
    r = admin.post("/admin/catalog/relations", data={
        "csrf_token": csrf(admin, "/admin/catalog"), "from_part_id": str(old_id), "to_part_id": str(new_id),
        "relation_type": "superseded_by", "confidence": "0.98", "notes": "Test replacement path", "source": "phase4 smoke test", "verified": "1",
    }, follow_redirects=False)
    assert r.status_code == 303
    with db() as conn:
        candidates = catalog_candidates(conn, "OLD100-A")
        assert candidates and candidates[0]["id"] == old_id and candidates[0]["match_reason"] == "Exact catalogue alias"

    # Add supplier inventory using the alias; catalogue linker should map it to canonical OLD-100.
    with db() as conn:
        supplier_company = conn.execute("SELECT company_id FROM users WHERE email='supplier@example.com'").fetchone()["company_id"]
        supplier_user = conn.execute("SELECT id FROM users WHERE email='supplier@example.com'").fetchone()["id"]
        from platform_core import insert_id, normalize, now_iso
        inv_id = insert_id(conn, """INSERT INTO inventory(company_id,brand,part_number,normalized_part,description,condition,quantity,location,active,created_at,created_by_user_id,updated_by_user_id,last_confirmed_at,updated_at)
            VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)""", (supplier_company, "Test Controls", "OLD100-A", normalize("OLD100-A"), "Alias stock", "New old stock", 2, "Birmingham, UK", 1, now_iso(), supplier_user, supplier_user, now_iso(), now_iso()))
    linked = link_inventory_ids([inv_id])
    assert linked["matched"] == 1
    with db() as conn:
        row = conn.execute("SELECT canonical_part_id,catalog_match_method FROM inventory WHERE id=?", (inv_id,)).fetchone()
        assert row["canonical_part_id"] == old_id and row["catalog_match_method"] == "alias"
        result = source_assistant(conn, "Need OLD100-A legacy controller")
        assert result["primary"] and result["primary"]["id"] == old_id
        assert result["replacements"] and result["replacements"][0]["id"] == new_id
        assert any(s["id"] == inv_id for s in result["stock"])

    # Catalogue detail renders the relationship and stock.
    detail = admin.get(f"/catalog/{old_id}")
    assert detail.status_code == 200 and "NEW-200" in detail.text and "OLD100-A" in detail.text and "Alias stock" not in detail.text

buyer = TestClient(app.app)
with buyer:
    login(buyer, "buyer@example.com", "Buyer123!")
    # Buyers can use sourcing/catalogue/photo-ID but cannot administer the catalogue.
    assert buyer.get("/catalog").status_code == 200
    assert buyer.get("/admin/catalog").status_code == 403
    r = buyer.post("/source", data={"csrf_token": csrf(buyer, "/source"), "query": "OLD100-A"})
    assert r.status_code == 200 and "NEW-200" in r.text and "2 matching lines" not in r.text
    # No OpenAI key: visible-text fallback still identifies via the local catalogue.
    r = buyer.post("/identify", data={"csrf_token": csrf(buyer, "/identify"), "visible_text": "Test Controls OLD100-A"}, files={"photo": ("label.jpg", b"not-a-real-image-but-not-read-in-local-mode", "image/jpeg")})
    assert r.status_code == 200 and "OLD-100" in r.text and "Local fallback mode" in r.text
    # Search inventory exposes canonical linkage.
    payload = buyer.get("/api/search", params={"q": "OLD100-A"}).json()
    match = next(x for x in payload["results"] if x["id"] == inv_id)
    assert match["catalog_part"]["id"] == old_id

# Simulate the live vision-provider HTTP response so request construction + parser are tested
# without consuming an API key in the automated suite.
import catalog_intelligence as ci
class FakeVisionResponse:
    status_code = 200
    def raise_for_status(self): return None
    def json(self):
        return {"output": [{"content": [{"type": "output_text", "text": '{"manufacturer":"Trend","part_number":"IQ233/UNB/230VAC","product_family":"IQ2","visible_text":"TREND IQ233","confidence":0.97,"notes":"Clear label"}'}]}]}
old_post = ci.httpx.post
os.environ["OPENAI_API_KEY"] = "test-key-not-sent"
def fake_post(url, **kwargs):
    assert url == "https://api.openai.com/v1/responses"
    assert kwargs["headers"]["Authorization"] == "Bearer test-key-not-sent"
    content = kwargs["json"]["input"][0]["content"]
    assert content[1]["type"] == "input_image" and content[1]["image_url"].startswith("data:image/jpeg;base64,")
    return FakeVisionResponse()
ci.httpx.post = fake_post
try:
    vision = ci.identify_photo(b"fake-jpeg", "image/jpeg", "label.jpg")
    assert vision["provider"].startswith("openai:") and vision["part_number"] == "IQ233/UNB/230VAC" and vision["confidence"] == 0.97

    class FakeIntentResponse:
        status_code = 200
        def raise_for_status(self): return None
        def json(self):
            return {"output": [{"content": [{"type": "output_text", "text": '{"manufacturer":"Trend","part_number":"IQ233/UNB/230VAC","keywords":["IQ233","UNB","230VAC"]}'}]}]}
    def fake_intent_post(url, **kwargs):
        assert url == "https://api.openai.com/v1/responses"
        assert "recommend replacements" in kwargs["json"]["input"]
        return FakeIntentResponse()
    ci.httpx.post = fake_intent_post
    intent = ci.extract_sourcing_intent("customer wants old Trend IQ233 controller")
    assert intent["provider"].startswith("openai:") and intent["part_number"] == "IQ233/UNB/230VAC" and "IQ233" in intent["keywords"]
finally:
    ci.httpx.post = old_post
    os.environ.pop("OPENAI_API_KEY", None)

print("PHASE4_SMOKE_TEST_OK")
