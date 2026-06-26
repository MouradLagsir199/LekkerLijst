"""Batch actions for catalog review candidates.

This is intentionally conservative: the default command approves only candidates
that are still pending and already have an AI recommendation of ``approve``.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import Any

import psycopg

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scrapers.bronze_ingest import connection_string


def fetch_candidate_ids(conn: psycopg.Connection, *, kind: str, limit: int) -> list[str]:
    where = ["c.status = 'pending'", "c.ai_decision->>'decision' = 'approve'"]
    params: list[Any] = []
    if kind in {"exact", "substitute"}:
        where.append("c.candidate_kind = %s")
        params.append(kind)
    params.append(limit)
    sql = f"""
      SELECT c.id::text
      FROM catalog.group_review_candidates c
      WHERE {' AND '.join(where)}
      ORDER BY c.ai_confidence DESC NULLS LAST, c.confidence DESC, c.created_at ASC
      LIMIT %s
    """
    with conn.cursor() as cur:
        cur.execute(sql, params)
        return [row[0] for row in cur.fetchall()]


def approve_batch(*, kind: str, limit: int, dry_run: bool) -> int:
    with psycopg.connect(connection_string(), prepare_threshold=None) as conn:
        candidate_ids = fetch_candidate_ids(conn, kind=kind, limit=limit)
        print(f"Found {len(candidate_ids)} pending AI-approved candidate(s)")
        if dry_run:
            for candidate_id in candidate_ids:
                print(candidate_id)
            return 0

        total_products = 0
        with conn.cursor() as cur:
            for candidate_id in candidate_ids:
                cur.execute(
                    "SELECT status, rows_affected FROM catalog.apply_group_review_candidate(%s, 'approve')",
                    (candidate_id,),
                )
                rows = cur.fetchall()
                total_products += sum(int(row[1] or 0) for row in rows)
        conn.commit()
        print(f"Approved {len(candidate_ids)} candidate(s), updated {total_products} public product row(s)")
        return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="Batch approve catalog review candidates.")
    parser.add_argument("--kind", choices=["all", "exact", "substitute"], default="all")
    parser.add_argument("--limit", type=int, default=100, help="Maximum candidates to approve")
    parser.add_argument("--dry-run", action="store_true", help="List candidates without approving them")
    args = parser.parse_args()
    if args.limit <= 0 or args.limit > 100:
        raise SystemExit("--limit must be between 1 and 100")
    return approve_batch(kind=args.kind, limit=args.limit, dry_run=args.dry_run)


if __name__ == "__main__":
    raise SystemExit(main())
