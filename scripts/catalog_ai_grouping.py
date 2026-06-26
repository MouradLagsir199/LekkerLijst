"""AI review for catalog grouping candidates.

This script does not publish groups. It reads pending rule-generated candidates,
sends silver-level product data to OpenAI, validates the structured response,
and stores the AI recommendation back on ``catalog.group_review_candidates``.

Required env:
  SUPABASE_DB_URL   direct/session-pooler Postgres URL
  OPENAI_API_KEY    OpenAI API key

Optional env:
  OPENAI_CATALOG_MODEL  defaults to gpt-5.4-mini, matching the repo's current AI config
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from typing import Any

import httpx
import psycopg
from psycopg.types.json import Jsonb

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scrapers.bronze_ingest import connection_string

OPENAI_URL = "https://api.openai.com/v1/responses"
DEFAULT_MODEL = "gpt-5.4-mini"

DECISION_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "required": ["decision", "group_kind", "confidence", "canonical_name", "reason", "safety_flags"],
    "properties": {
        "decision": {"type": "string", "enum": ["approve", "reject", "needs_review"]},
        "group_kind": {"type": "string", "enum": ["exact", "substitute"]},
        "confidence": {"type": "number", "minimum": 0, "maximum": 1},
        "canonical_name": {"type": "string"},
        "reason": {"type": "string"},
        "safety_flags": {
            "type": "object",
            "additionalProperties": False,
            "required": [
                "diet_or_quality_variant_mixed",
                "package_mismatch",
                "brand_or_private_label_mismatch",
                "insufficient_information",
            ],
            "properties": {
                "diet_or_quality_variant_mixed": {"type": "boolean"},
                "package_mismatch": {"type": "boolean"},
                "brand_or_private_label_mismatch": {"type": "boolean"},
                "insufficient_information": {"type": "boolean"},
            },
        },
    },
}


def fetch_candidates(conn: psycopg.Connection, *, limit: int, kind: str | None) -> list[dict[str, Any]]:
    where_kind = "" if kind is None else "AND c.candidate_kind = %s"
    params: list[Any] = []
    if kind is not None:
        params.append(kind)
    params.append(limit)

    query = f"""
      SELECT
        c.id::text,
        c.candidate_kind,
        c.candidate_key,
        c.canonical_name,
        c.confidence::float,
        c.reason,
        jsonb_agg(
          jsonb_build_object(
            'silver_product_id', sp.id::text,
            'store', sp.store,
            'external_id', sp.external_id,
            'name', sp.name,
            'ean', sp.ean,
            'price', sp.price,
            'base_price', sp.base_price,
            'base_price_unit', sp.base_price_unit,
            'image_url', sp.image_url
          )
          ORDER BY m.position, sp.store, sp.name
        ) AS products
      FROM catalog.group_review_candidates c
      JOIN catalog.group_review_candidate_members m ON m.candidate_id = c.id
      JOIN catalog.silver_products sp ON sp.id = m.silver_product_id
      WHERE c.status = 'pending'
        AND c.ai_decision IS NULL
        {where_kind}
      GROUP BY c.id
      ORDER BY c.confidence DESC, c.created_at ASC
      LIMIT %s
    """
    with conn.cursor() as cur:
        cur.execute(query, params)
        rows = cur.fetchall()
    return [
        {
            "id": row[0],
            "candidate_kind": row[1],
            "candidate_key": row[2],
            "canonical_name": row[3],
            "confidence": row[4],
            "reason": row[5],
            "products": row[6],
        }
        for row in rows
    ]


def extract_output_text(response_json: dict[str, Any]) -> str | None:
    for item in response_json.get("output") or []:
        for content in item.get("content") or []:
            if content.get("type") in {"output_text", "text"} and content.get("text"):
                return content["text"]
    if isinstance(response_json.get("output_text"), str):
        return response_json["output_text"]
    return None


def validate_decision(candidate: dict[str, Any], decision: dict[str, Any]) -> dict[str, Any]:
    if decision.get("decision") not in {"approve", "reject", "needs_review"}:
        raise ValueError("decision must be approve, reject, or needs_review")
    if decision.get("group_kind") != candidate["candidate_kind"]:
        raise ValueError("AI group_kind must match the queued candidate kind")
    confidence = decision.get("confidence")
    if not isinstance(confidence, (int, float)) or not 0 <= confidence <= 1:
        raise ValueError("confidence must be a number between 0 and 1")
    if not isinstance(decision.get("canonical_name"), str) or not decision["canonical_name"].strip():
        raise ValueError("canonical_name must be a non-empty string")
    if not isinstance(decision.get("reason"), str) or not decision["reason"].strip():
        raise ValueError("reason must be a non-empty string")
    flags = decision.get("safety_flags")
    if not isinstance(flags, dict):
        raise ValueError("safety_flags must be an object")
    for key in DECISION_SCHEMA["properties"]["safety_flags"]["required"]:
        if not isinstance(flags.get(key), bool):
            raise ValueError(f"safety_flags.{key} must be boolean")
    return decision


def ask_openai(client: httpx.Client, *, api_key: str, model: str, candidate: dict[str, Any]) -> dict[str, Any]:
    system = (
        "You review Dutch supermarket product grouping candidates. "
        "For exact groups, approve only if every product is the same physical product/SKU. "
        "For substitute groups, approve only if products are the same generic product and same meaningful spec; brand may differ. "
        "Never merge organic, lactose-free, vegan, halal, gluten-free, alcohol-free, baby, pet, diet/light/zero, or other quality/diet variants with regular variants unless all products share that variant. "
        "Reject package-size or unit mismatches. Return only the requested JSON."
    )
    payload = {
        "candidate": candidate,
        "note": "These are silver normalized product rows only; no bronze raw JSON is included.",
    }
    response = client.post(
        OPENAI_URL,
        headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
        json={
            "model": model,
            "input": [
                {"role": "system", "content": system},
                {"role": "user", "content": json.dumps(payload, ensure_ascii=False)},
            ],
            "text": {
                "format": {
                    "type": "json_schema",
                    "name": "catalog_group_decision",
                    "strict": True,
                    "schema": DECISION_SCHEMA,
                }
            },
        },
        timeout=120,
    )
    body = response.json()
    if response.status_code >= 400:
        raise RuntimeError(body.get("error", {}).get("message") or body)
    output_text = extract_output_text(body)
    if not output_text:
        raise RuntimeError("OpenAI response did not contain output text")
    return validate_decision(candidate, json.loads(output_text))


def store_decision(conn: psycopg.Connection, *, candidate_id: str, model: str, decision: dict[str, Any]) -> None:
    with conn.cursor() as cur:
        cur.execute(
            """
            UPDATE catalog.group_review_candidates
               SET source = 'ai',
                   ai_model = %s,
                   ai_decision = %s,
                   ai_confidence = %s,
                   ai_reason = %s,
                   safety_flags = %s,
                   updated_at = now()
             WHERE id = %s
            """,
            (
                model,
                Jsonb(decision),
                decision["confidence"],
                decision["reason"],
                Jsonb(decision["safety_flags"]),
                candidate_id,
            ),
        )
    conn.commit()


def main() -> int:
    parser = argparse.ArgumentParser(description="Run AI review for catalog grouping candidates.")
    parser.add_argument("--limit", type=int, default=25, help="Maximum candidates to review")
    parser.add_argument("--kind", choices=["exact", "substitute", "all"], default="all")
    parser.add_argument("--dry-run", action="store_true", help="Fetch candidates but do not call OpenAI or write decisions")
    args = parser.parse_args()

    if args.limit <= 0:
        raise SystemExit("--limit must be positive")

    api_key = os.environ.get("OPENAI_API_KEY")
    if not api_key and not args.dry_run:
        raise SystemExit("Missing OPENAI_API_KEY. Add it locally or as a GitHub secret before running AI grouping.")
    model = os.environ.get("OPENAI_CATALOG_MODEL") or DEFAULT_MODEL

    with psycopg.connect(connection_string(), prepare_threshold=None) as conn:
        candidates = fetch_candidates(conn, limit=args.limit, kind=None if args.kind == "all" else args.kind)
        print(f"Fetched {len(candidates)} pending candidate(s)")
        if args.dry_run:
            for candidate in candidates[:5]:
                print(json.dumps(candidate, ensure_ascii=False)[:2000])
            return 0

        assert api_key is not None
        with httpx.Client() as client:
            for index, candidate in enumerate(candidates, start=1):
                print(f"[{index}/{len(candidates)}] reviewing {candidate['candidate_kind']} {candidate['id']}")
                decision = ask_openai(client, api_key=api_key, model=model, candidate=candidate)
                store_decision(conn, candidate_id=candidate["id"], model=model, decision=decision)
                print(f"  -> {decision['decision']} ({decision['confidence']:.2f}) {decision['reason'][:160]}")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        sys.exit(130)
