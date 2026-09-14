from __future__ import annotations

import base64
import json
import os
import re
from typing import Any

import httpx

from platform_core import db, insert_id, normalize, now_iso, inventory_match_score, freshness_cutoff_iso, logger
from billing import commercial_access

CATALOG_RELATION_TYPES = {
    "superseded_by": "Superseded by",
    "supersedes": "Supersedes",
    "compatible_with": "Compatible with",
    "alternative_to": "Alternative to",
    "requires_adapter": "Requires adapter",
}
LIFECYCLE_STATUSES = ("current", "legacy", "obsolete", "discontinued", "unknown")


def _as_dict(row: Any) -> dict[str, Any]:
    return dict(row) if row is not None else {}


def seed_demo_catalog() -> None:
    """Seed only minimal demo catalogue identities for the seeded demo inventory.

    We intentionally do not seed technical compatibility/supersession claims. Those need a
    verified source or admin review before they should appear as technical guidance.
    """
    demo = [
        ("Trend", "IQ233/UNB/230VAC", "IQ233 controller", "IQ2", "IQ2 series universal network controller"),
        ("Honeywell", "XFL521B", "XFL521B I/O module", "Excel 500", "Excel 500 I/O module"),
        ("Johnson Controls", "MS-NAE5510-2", "NAE5510 network automation engine", "Metasys", "Metasys network automation engine"),
        ("Schneider", "TAC XENTA 401", "TAC Xenta 401 controller", "TAC Xenta", "Programmable HVAC controller"),
        ("Siemens", "PXC64-U", "PXC64-U automation station", "DESIGO", "DESIGO automation station"),
        ("ABB / Cylon", "UC32.24", "UC32.24 field controller", "Unitron UC32", "Unitron UC32 field controller"),
    ]
    with db() as conn:
        for manufacturer, part, name, family, description in demo:
            existing = conn.execute(
                "SELECT id FROM catalog_parts WHERE lower(manufacturer)=? AND normalized_part=?",
                (manufacturer.lower(), normalize(part)),
            ).fetchone()
            if existing:
                continue
            insert_id(conn, """INSERT INTO catalog_parts(manufacturer,part_number,normalized_part,name,product_family,description,lifecycle_status,notes,source,verified,created_at,updated_at)
                VALUES(?,?,?,?,?,?,?,?,?,?,?,?)""",
                (manufacturer, part, normalize(part), name, family, description, "unknown", "Demo catalogue identity only; technical status not asserted.", "demo seed", 0, now_iso(), now_iso()))


def catalog_candidates(conn, query: str, manufacturer: str = "", limit: int = 20) -> list[dict[str, Any]]:
    q = (query or "").strip()
    nq = normalize(q)
    if len(nq) < 2:
        return []
    broad = nq[:3]
    params: list[Any] = [f"%{nq}%", f"%{nq}%", f"%{q.lower()}%", f"%{q.lower()}%", f"%{broad}%"]
    extra = ""
    if manufacturer:
        extra = " AND lower(p.manufacturer)=?"
        params.append(manufacturer.lower())
    rows = conn.execute(
        f"""SELECT DISTINCT p.* FROM catalog_parts p
        LEFT JOIN catalog_aliases a ON a.part_id=p.id
        WHERE (p.normalized_part LIKE ? OR a.normalized_alias LIKE ? OR lower(p.manufacturer) LIKE ?
               OR lower(p.name) LIKE ? OR p.normalized_part LIKE ?) {extra}
        LIMIT 250""", tuple(params)).fetchall()
    ranked: list[dict[str, Any]] = []
    aliases_by_part: dict[int, list[str]] = {}
    if rows:
        ids = [r["id"] for r in rows]
        aliases = conn.execute(f"SELECT part_id,alias FROM catalog_aliases WHERE part_id IN ({','.join('?' for _ in ids)})", ids).fetchall()
        for a in aliases:
            aliases_by_part.setdefault(a["part_id"], []).append(a["alias"])
    for row in rows:
        item = dict(row)
        proxy = {
            "part_number": item["part_number"],
            "brand": item["manufacturer"],
            "description": f"{item.get('name','')} {item.get('product_family','')} {item.get('description','')} {' '.join(aliases_by_part.get(item['id'], []))}",
        }
        score, reason = inventory_match_score(q, proxy)
        alias_norms = [normalize(a) for a in aliases_by_part.get(item["id"], [])]
        if nq in alias_norms:
            score, reason = 99.0, "Exact catalogue alias"
        if score < 45:
            continue
        item["match_score"] = score
        item["match_reason"] = reason
        item["aliases"] = aliases_by_part.get(item["id"], [])
        ranked.append(item)
    ranked.sort(key=lambda x: (-x["match_score"], -int(bool(x.get("verified"))), x["manufacturer"], x["part_number"]))
    return ranked[:limit]


def resolve_catalog_part(conn, manufacturer: str, part_number: str) -> tuple[int | None, str, float]:
    np = normalize(part_number)
    if not np:
        return None, "none", 0.0
    exact = conn.execute(
        "SELECT id FROM catalog_parts WHERE normalized_part=? AND (?='' OR lower(manufacturer)=lower(?)) ORDER BY verified DESC,id LIMIT 1",
        (np, manufacturer, manufacturer),
    ).fetchone()
    if exact:
        return int(exact["id"]), "exact", 1.0
    alias = conn.execute(
        """SELECT p.id FROM catalog_aliases a JOIN catalog_parts p ON p.id=a.part_id
        WHERE a.normalized_alias=? AND (?='' OR lower(p.manufacturer)=lower(?)) ORDER BY a.verified DESC,p.verified DESC,p.id LIMIT 1""",
        (np, manufacturer, manufacturer),
    ).fetchone()
    if alias:
        return int(alias["id"]), "alias", 0.99
    candidates = catalog_candidates(conn, part_number, manufacturer=manufacturer, limit=3)
    if candidates and candidates[0]["match_score"] >= 91:
        return int(candidates[0]["id"]), "fuzzy", round(candidates[0]["match_score"] / 100, 3)
    return None, "none", 0.0


def link_inventory_ids(inventory_ids: list[int] | None = None) -> dict[str, int]:
    updated = matched = unmatched = 0
    with db() as conn:
        sql = "SELECT id,brand,part_number,canonical_part_id,catalog_match_method,catalog_match_confidence FROM inventory WHERE deleted_at IS NULL"
        params: list[Any] = []
        if inventory_ids:
            clean = sorted({int(i) for i in inventory_ids if i})
            if not clean:
                return {"updated": 0, "matched": 0, "unmatched": 0}
            sql += f" AND id IN ({','.join('?' for _ in clean)})"
            params.extend(clean)
        rows = conn.execute(sql, params).fetchall()
        for row in rows:
            part_id, method, confidence = resolve_catalog_part(conn, row["brand"] or "", row["part_number"] or "")
            if part_id:
                matched += 1
            else:
                unmatched += 1
            old_conf = float(row["catalog_match_confidence"] or 0)
            if row["canonical_part_id"] != part_id or (row["catalog_match_method"] or "none") != method or abs(old_conf-confidence) > 0.0001:
                conn.execute("UPDATE inventory SET canonical_part_id=?,catalog_match_method=?,catalog_match_confidence=? WHERE id=?", (part_id, method, confidence, row["id"]))
                updated += 1
    return {"updated": updated, "matched": matched, "unmatched": unmatched}


def part_detail(conn, part_id: int) -> dict[str, Any] | None:
    row = conn.execute("SELECT * FROM catalog_parts WHERE id=?", (part_id,)).fetchone()
    if not row:
        return None
    part = dict(row)
    part["aliases"] = [dict(r) for r in conn.execute("SELECT * FROM catalog_aliases WHERE part_id=? ORDER BY verified DESC,alias", (part_id,)).fetchall()]
    rels = conn.execute("""SELECT r.*,p.manufacturer AS target_manufacturer,p.part_number AS target_part_number,p.name AS target_name,p.lifecycle_status AS target_lifecycle
        FROM catalog_relations r JOIN catalog_parts p ON p.id=r.to_part_id WHERE r.from_part_id=?
        ORDER BY r.verified DESC,r.confidence DESC,p.manufacturer,p.part_number""", (part_id,)).fetchall()
    part["relations"] = [dict(r) for r in rels]
    reverse = conn.execute("""SELECT r.*,p.manufacturer AS source_manufacturer,p.part_number AS source_part_number,p.name AS source_name
        FROM catalog_relations r JOIN catalog_parts p ON p.id=r.from_part_id WHERE r.to_part_id=?
        ORDER BY r.verified DESC,r.confidence DESC,p.manufacturer,p.part_number""", (part_id,)).fetchall()
    part["reverse_relations"] = [dict(r) for r in reverse]
    stock = conn.execute("""SELECT i.id,i.company_id,i.condition,i.quantity,i.location,i.last_confirmed_at,c.name AS supplier_name
        FROM inventory i JOIN companies c ON c.id=i.company_id
        WHERE i.canonical_part_id=? AND i.active=1 AND i.deleted_at IS NULL AND i.quantity>0
          AND i.last_confirmed_at>=? AND c.active=1 AND c.verified=1
        ORDER BY i.last_confirmed_at DESC""", (part_id, freshness_cutoff_iso())).fetchall()
    part["stock"] = [dict(r) for r in stock if commercial_access(conn, int(r["company_id"]))]
    return part


def technical_recommendations(conn, part_id: int) -> dict[str, list[dict[str, Any]]]:
    detail = part_detail(conn, part_id)
    result = {"replacements": [], "alternatives": [], "requirements": []}
    if not detail:
        return result
    for rel in detail["relations"]:
        target = {
            "id": rel["to_part_id"], "manufacturer": rel["target_manufacturer"], "part_number": rel["target_part_number"],
            "name": rel["target_name"], "lifecycle_status": rel["target_lifecycle"], "relation_type": rel["relation_type"],
            "confidence": rel["confidence"], "verified": rel["verified"], "notes": rel["notes"], "source": rel["source"],
        }
        if rel["relation_type"] in {"superseded_by", "supersedes"}:
            result["replacements"].append(target)
        elif rel["relation_type"] in {"compatible_with", "alternative_to"}:
            result["alternatives"].append(target)
        else:
            result["requirements"].append(target)
    return result


def source_assistant(conn, query: str, company_id: int | None = None) -> dict[str, Any]:
    q = (query or "").strip()
    intent = extract_sourcing_intent(q)
    # Start with the full query, then probe AI/local extracted identifiers and part-like tokens.
    # The model is only allowed to improve retrieval. Technical recommendations below remain
    # grounded in catalog_relations created/reviewed by platform admins.
    combined: dict[int, dict[str, Any]] = {}
    probes = [q, intent.get("part_number", "")] + list(intent.get("keywords", []) or [])
    probes += [t for t in re.findall(r"[A-Za-z0-9][A-Za-z0-9./:_-]{2,}", q) if any(ch.isdigit() for ch in t)]
    seen_probes: set[str] = set()
    for probe in probes:
        probe = str(probe or "").strip()
        if len(normalize(probe)) < 2 or probe.lower() in seen_probes:
            continue
        seen_probes.add(probe.lower())
        for candidate in catalog_candidates(conn, probe, manufacturer=str(intent.get("manufacturer", "") or "") if probe == intent.get("part_number") else "", limit=8):
            current = combined.get(candidate["id"])
            if current is None or candidate["match_score"] > current["match_score"]:
                combined[candidate["id"]] = candidate
    candidates = sorted(combined.values(), key=lambda x: (-x["match_score"], -int(bool(x.get("verified"))), x["manufacturer"], x["part_number"]))[:8]
    primary = candidates[0] if candidates and candidates[0]["match_score"] >= 55 else None
    result: dict[str, Any] = {
        "query": q,
        "intent": intent,
        "primary": primary,
        "catalog_candidates": candidates[:5],
        "replacements": [], "alternatives": [], "requirements": [], "stock": [],
        "summary": "No catalogue match yet. Try a model number, manufacturer, visible label text, or create a wanted request.",
    }
    if primary:
        recs = technical_recommendations(conn, primary["id"])
        result.update(recs)
        stock = conn.execute("""SELECT i.id,i.company_id,i.brand,i.part_number,i.condition,i.quantity,i.location,i.last_confirmed_at,c.name AS supplier_name
            FROM inventory i JOIN companies c ON c.id=i.company_id
            WHERE i.canonical_part_id=? AND i.active=1 AND i.deleted_at IS NULL AND i.quantity>0 AND i.last_confirmed_at>=?
              AND c.active=1 AND c.verified=1 """ + ("AND i.company_id!=? " if company_id else "") + "ORDER BY i.last_confirmed_at DESC LIMIT 30",
            (primary["id"], freshness_cutoff_iso(), company_id) if company_id else (primary["id"], freshness_cutoff_iso())).fetchall()
        result["stock"] = [dict(r) for r in stock if commercial_access(conn, int(r["company_id"]))]
        pieces = [f"Best catalogue match: {primary['manufacturer']} {primary['part_number']} ({primary['match_reason'].lower()})."]
        if stock:
            pieces.append(f"{len(stock)} searchable stock line{'s' if len(stock)!=1 else ''} currently match this canonical part.")
        if recs["replacements"]:
            pieces.append(f"{len(recs['replacements'])} recorded replacement/supersession relationship{'s' if len(recs['replacements'])!=1 else ''} available.")
        if recs["alternatives"]:
            pieces.append(f"{len(recs['alternatives'])} recorded compatible/alternative relationship{'s' if len(recs['alternatives'])!=1 else ''} available.")
        result["summary"] = " ".join(pieces)
    return result


def _extract_openai_output_text(payload: dict[str, Any]) -> str:
    texts: list[str] = []
    for item in payload.get("output", []) or []:
        for content in item.get("content", []) or []:
            if content.get("type") == "output_text" and content.get("text"):
                texts.append(str(content["text"]))
    return "\n".join(texts).strip()


def extract_sourcing_intent(query: str) -> dict[str, Any]:
    """Extract search terms from natural language; never generate compatibility advice here."""
    local_tokens = [t for t in re.findall(r"[A-Za-z0-9][A-Za-z0-9./:_-]{2,}", query or "") if any(ch.isdigit() for ch in t)]
    fallback = {"provider": "local", "manufacturer": "", "part_number": local_tokens[0] if local_tokens else "", "keywords": local_tokens[:5]}
    api_key = os.getenv("OPENAI_API_KEY", "").strip()
    if not api_key or os.getenv("AI_SOURCING_ENABLED", "true").lower() != "true":
        return fallback
    model = os.getenv("AI_SOURCING_MODEL", os.getenv("AI_MODEL", "gpt-5.6-luna")).strip()
    prompt = """Extract sourcing identifiers from this industrial BMS/HVAC controls request. Return ONLY compact JSON with keys manufacturer, part_number, keywords. Do not recommend replacements, compatibility, wiring, firmware or installation changes. If a field is uncertain, leave it blank. keywords must be an array of short strings. Request: """ + query
    try:
        response = httpx.post(
            "https://api.openai.com/v1/responses",
            headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
            json={"model": model, "input": prompt}, timeout=30,
        )
        response.raise_for_status()
        text = _extract_openai_output_text(response.json())
        parsed = json.loads(text)
        keywords = parsed.get("keywords") if isinstance(parsed.get("keywords"), list) else []
        return {
            "provider": f"openai:{model}",
            "manufacturer": str(parsed.get("manufacturer", "") or "").strip(),
            "part_number": str(parsed.get("part_number", "") or "").strip(),
            "keywords": [str(k).strip() for k in keywords if str(k).strip()][:8],
        }
    except Exception:
        logger.exception("sourcing_intent_extraction_failed")
        return fallback


def identify_photo(image_bytes: bytes, content_type: str, filename: str, visible_text_hint: str = "") -> dict[str, Any]:
    """Identify an industrial control from a photo.

    With OPENAI_API_KEY configured, image bytes are sent directly to the Responses API and are
    not stored by Controls Exchange. Without a key, visible label text is matched locally.
    """
    allowed = {"image/jpeg", "image/png", "image/webp"}
    if content_type not in allowed:
        raise ValueError("Upload a JPEG, PNG or WebP image.")
    if not image_bytes:
        raise ValueError("The uploaded image was empty.")
    max_bytes = int(os.getenv("PHOTO_ID_MAX_BYTES", str(8 * 1024 * 1024)))
    if len(image_bytes) > max_bytes:
        raise ValueError(f"Photo is too large. Maximum size is {max_bytes // (1024*1024)} MB.")

    api_key = os.getenv("OPENAI_API_KEY", "").strip()
    if not api_key:
        hint = visible_text_hint.strip()
        if len(normalize(hint)) < 2:
            return {"provider": "local", "configured": False, "manufacturer": "", "part_number": "", "visible_text": hint,
                    "confidence": 0.0, "notes": "Photo AI is not configured. Add OPENAI_API_KEY or type visible label/model text for local catalogue matching."}
        with db() as conn:
            combined: dict[int, dict[str, Any]] = {}
            for candidate in catalog_candidates(conn, hint, limit=5):
                combined[candidate["id"]] = candidate
            for token in re.findall(r"[A-Za-z0-9][A-Za-z0-9./:_-]{2,}", hint):
                if not any(ch.isdigit() for ch in token):
                    continue
                for candidate in catalog_candidates(conn, token, limit=3):
                    current = combined.get(candidate["id"])
                    if current is None or candidate["match_score"] > current["match_score"]:
                        combined[candidate["id"]] = candidate
            candidates = sorted(combined.values(), key=lambda x: (-x["match_score"], -int(bool(x.get("verified")))))
        best = candidates[0] if candidates else None
        return {"provider": "local", "configured": False, "manufacturer": best["manufacturer"] if best else "",
                "part_number": best["part_number"] if best else "", "visible_text": hint,
                "confidence": round((best["match_score"] / 100), 3) if best else 0.0,
                "notes": "Matched from the visible-text hint only; the image itself was not analysed because OPENAI_API_KEY is not configured."}

    model = os.getenv("PHOTO_ID_MODEL", os.getenv("AI_MODEL", "gpt-5.6-luna")).strip()
    data_url = f"data:{content_type};base64,{base64.b64encode(image_bytes).decode('ascii')}"
    prompt = """Identify the building-management / HVAC controls component in this photo. Read labels carefully. Return ONLY compact JSON with keys: manufacturer, part_number, product_family, visible_text, confidence, notes. confidence must be 0 to 1. Do not invent a part number: use an empty string if unreadable. Mention uncertainty in notes."""
    if visible_text_hint.strip():
        prompt += f"\nUser-provided visible-label hint: {visible_text_hint.strip()}"
    response = httpx.post(
        "https://api.openai.com/v1/responses",
        headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
        json={"model": model, "input": [{"role": "user", "content": [{"type": "input_text", "text": prompt}, {"type": "input_image", "image_url": data_url, "detail": "high"}]}]},
        timeout=60,
    )
    response.raise_for_status()
    text = _extract_openai_output_text(response.json())
    try:
        parsed = json.loads(text)
    except Exception:
        match = re.search(r"\{.*\}", text, re.S)
        if not match:
            raise ValueError("Photo identification service returned an unreadable response.")
        parsed = json.loads(match.group(0))
    return {
        "provider": f"openai:{model}", "configured": True,
        "manufacturer": str(parsed.get("manufacturer", "") or "").strip(),
        "part_number": str(parsed.get("part_number", "") or "").strip(),
        "product_family": str(parsed.get("product_family", "") or "").strip(),
        "visible_text": str(parsed.get("visible_text", "") or visible_text_hint).strip(),
        "confidence": max(0.0, min(1.0, float(parsed.get("confidence", 0) or 0))),
        "notes": str(parsed.get("notes", "") or "").strip(),
    }


def match_identification_to_catalog(result: dict[str, Any]) -> dict[str, Any]:
    query = " ".join(x for x in [result.get("manufacturer", ""), result.get("part_number", ""), result.get("visible_text", "")] if x).strip()
    with db() as conn:
        candidates = catalog_candidates(conn, result.get("part_number") or query, manufacturer=result.get("manufacturer", ""), limit=5)
    result["catalog_candidates"] = candidates
    result["matched_part_id"] = candidates[0]["id"] if candidates and candidates[0]["match_score"] >= 65 else None
    return result
