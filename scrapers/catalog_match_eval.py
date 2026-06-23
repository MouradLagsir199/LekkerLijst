"""Evaluate the product alternatives returned by the live catalog search.

The deterministic phase verifies that the price-first default stays inside a
relevance band. The optional AI phase reviews only the small candidate sets a
user can see; the complete product catalog never leaves Postgres.
"""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
from typing import Any

import requests

from catalog_gold import OPENAI_URL, openai_headers, output_text
from catalog_pipeline import SupabaseApi, env_or_fail


DEFAULT_CASES = [
    {"ingredient": "halfvolle melk", "expectedTerms": ["melk"]},
    {"ingredient": "kipfilet", "expectedTerms": ["kip", "filet"]},
    {"ingredient": "boter", "expectedTerms": ["boter", "margarine"]},
    {"ingredient": "chocoladepasta", "expectedTerms": ["chocolade", "pasta"]},
    {"ingredient": "spaghetti", "expectedTerms": ["spaghetti"]},
    {"ingredient": "tomaten", "expectedTerms": ["tomaat"]},
    {"ingredient": "knoflook", "expectedTerms": ["knoflook"]},
    {"ingredient": "olijfolie", "expectedTerms": ["olijf", "olie"]},
    {"ingredient": "eieren", "expectedTerms": ["ei"]},
    {"ingredient": "bloem", "expectedTerms": ["bloem"]},
]

AI_REVIEW_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "required": ["decisions"],
    "properties": {
        "decisions": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": ["ingredient", "verdict", "selectedProductId", "reason"],
                "properties": {
                    "ingredient": {"type": "string"},
                    "verdict": {"type": "string", "enum": ["pass", "fail", "uncertain"]},
                    "selectedProductId": {"type": ["string", "null"]},
                    "reason": {"type": "string", "maxLength": 240},
                },
            },
        }
    },
}


def write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def select_lowest_priced_relevant(candidates: list[dict[str, Any]]) -> dict[str, Any] | None:
    valid = [
        candidate
        for candidate in candidates
        if candidate.get("current_price_cents") is not None and candidate.get("match_score") is not None
    ]
    if not valid:
        return None
    best_score = max(float(candidate["match_score"]) for candidate in valid)
    relevance_floor = max(0.35, best_score - 0.15)
    relevant = [candidate for candidate in valid if float(candidate["match_score"]) >= relevance_floor]
    return min(
        relevant,
        key=lambda candidate: (int(candidate["current_price_cents"]), -float(candidate["match_score"]), candidate["product_name"]),
    )


def candidate_matches_expected(candidate: dict[str, Any] | None, expected_terms: list[str]) -> bool:
    if not candidate:
        return False
    text = " ".join(str(candidate.get(field) or "") for field in ("product_name", "brand", "category")).lower()
    return any(term.lower() in text for term in expected_terms)


def evaluate_cases(api: SupabaseApi, cases: list[dict[str, Any]]) -> dict[str, Any]:
    results: list[dict[str, Any]] = []
    for case in cases:
        candidates = api.rpc(
            "search_products",
            {"query_text": case["ingredient"], "store_filter": None, "match_count": 8},
        )
        if not isinstance(candidates, list):
            raise RuntimeError(f"search_products returned a non-list for {case['ingredient']!r}")
        selected = select_lowest_priced_relevant(candidates)
        results.append(
            {
                "ingredient": case["ingredient"],
                "expectedTerms": case["expectedTerms"],
                "selectedCandidate": selected,
                "candidates": candidates,
                "candidateCount": len(candidates),
                "deterministicPass": candidate_matches_expected(selected, case["expectedTerms"]),
            }
        )

    passed = sum(1 for result in results if result["deterministicPass"])
    return {
        "caseCount": len(results),
        "deterministicPassCount": passed,
        "deterministicPassRate": passed / len(results) if results else 0,
        "cases": results,
    }


def ai_review_payload(report: dict[str, Any]) -> list[dict[str, Any]]:
    return [
        {
            "ingredient": case["ingredient"],
            "expectedTerms": case["expectedTerms"],
            "selectedCandidate": case.get("selectedCandidate"),
            "candidates": case.get("candidates", []),
        }
        for case in report.get("cases", [])
    ]


def run_ai_review(report: dict[str, Any], model: str) -> dict[str, Any]:
    instructions = (
        "You are reviewing Dutch supermarket alternatives for recipe ingredients. "
        "Judge whether the selected cheapest candidate is a sensible product for the requested ingredient, "
        "using the listed alternatives as context. A broad culinary mapping is acceptable: margarine can "
        "serve as boter and a branded product can serve as its generic ingredient. Mark fail when the chosen "
        "item is materially different. Mark uncertain only when the candidate data is insufficient. "
        "Return one decision per input case in concise Dutch."
    )
    response = requests.post(
        f"{OPENAI_URL}/responses",
        headers={**openai_headers(), "Content-Type": "application/json"},
        json={
            "model": model,
            "input": [
                {"role": "system", "content": instructions},
                {"role": "user", "content": json.dumps(ai_review_payload(report), ensure_ascii=False)},
            ],
            "text": {"format": {"type": "json_schema", "name": "catalog_match_review", "strict": True, "schema": AI_REVIEW_SCHEMA}},
        },
        timeout=120,
    )
    response.raise_for_status()
    text = output_text(response.json())
    if not text:
        raise RuntimeError("OpenAI returned no structured catalog review")
    review = json.loads(text)
    decisions = review.get("decisions")
    if not isinstance(decisions, list):
        raise RuntimeError("OpenAI catalog review is missing decisions")
    passed = sum(1 for decision in decisions if decision.get("verdict") == "pass")
    return {
        "model": model,
        "decisionCount": len(decisions),
        "passCount": passed,
        "passRate": passed / len(decisions) if decisions else 0,
        "decisions": decisions,
    }


def load_cases(path: Path | None) -> list[dict[str, Any]]:
    if not path:
        return DEFAULT_CASES
    cases = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(cases, list) or not all(isinstance(case, dict) for case in cases):
        raise SystemExit("Cases must be a JSON array of ingredient/expectedTerms objects.")
    return cases


def main() -> None:
    parser = argparse.ArgumentParser(description="Evaluate live product alternatives after gold categorization.")
    subparsers = parser.add_subparsers(dest="command", required=True)

    evaluate_parser = subparsers.add_parser("evaluate")
    evaluate_parser.add_argument("--out", type=Path, required=True)
    evaluate_parser.add_argument("--cases", type=Path)
    evaluate_parser.add_argument("--min-pass-rate", type=float, default=0.8)

    review_parser = subparsers.add_parser("ai-review")
    review_parser.add_argument("--report", type=Path, required=True)
    review_parser.add_argument("--out", type=Path, required=True)
    review_parser.add_argument("--model", default=os.environ.get("OPENAI_EVAL_MODEL", "gpt-5.4-mini"))
    review_parser.add_argument("--min-pass-rate", type=float, default=0.8)

    args = parser.parse_args()
    if args.command == "evaluate":
        api = SupabaseApi(env_or_fail("SUPABASE_URL"), env_or_fail("SUPABASE_SERVICE_ROLE_KEY"))
        report = evaluate_cases(api, load_cases(args.cases))
        write_json(args.out, report)
        print(json.dumps({key: report[key] for key in ("caseCount", "deterministicPassCount", "deterministicPassRate")}, indent=2))
        if report["deterministicPassRate"] < args.min_pass_rate:
            raise SystemExit("Deterministic catalog match quality is below the required threshold.")
    elif args.command == "ai-review":
        review = run_ai_review(json.loads(args.report.read_text(encoding="utf-8")), args.model)
        write_json(args.out, review)
        print(json.dumps({key: review[key] for key in ("model", "decisionCount", "passCount", "passRate")}, indent=2))
        if review["passRate"] < args.min_pass_rate:
            raise SystemExit("AI catalog match quality is below the required threshold.")


if __name__ == "__main__":
    main()
