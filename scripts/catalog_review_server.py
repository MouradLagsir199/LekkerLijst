"""Local-only catalog grouping review server.

Run from the repo root:
  .venv/Scripts/python.exe scripts/catalog_review_server.py --port 8094

It uses SUPABASE_DB_URL and direct Postgres access. Do not deploy this as part
of the mobile app; it is intentionally an operator/admin tool.
"""

from __future__ import annotations

import argparse
import html
import json
import re
import sys
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse

import psycopg

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scrapers.bronze_ingest import connection_string

UUID_RE = re.compile(r"^[0-9a-fA-F-]{36}$")


def h(value: Any) -> str:
    return html.escape("" if value is None else str(value), quote=True)


def money(value: int | float | None) -> str:
    if value is None:
        return "-"
    return f"EUR {float(value):.2f}"


def cents(value: int | None) -> str:
    if value is None:
        return "-"
    return f"EUR {value / 100:.2f}"


class ReviewStore:
    def __init__(self) -> None:
        self.conninfo = connection_string()

    def connect(self) -> psycopg.Connection:
        return psycopg.connect(self.conninfo, prepare_threshold=None)

    def list_candidates(self, *, status: str, kind: str, limit: int) -> list[dict[str, Any]]:
        where = ["c.status = %s"]
        params: list[Any] = [status]
        if kind in {"exact", "substitute"}:
            where.append("c.candidate_kind = %s")
            params.append(kind)
        params.append(limit)
        sql = f"""
          SELECT
            c.id::text,
            c.candidate_kind,
            c.status,
            c.canonical_name,
            c.confidence::float,
            c.reason,
            c.ai_model,
            c.ai_confidence::float,
            c.ai_reason,
            c.safety_flags,
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
                'image_url', sp.image_url,
                'public_product_id', pp.id::text,
                'current_price_cents', pp.current_price_cents,
                'unit_price_cents', pp.unit_price_cents,
                'unit_price_unit', pp.unit_price_unit
              )
              ORDER BY m.position, sp.store, sp.name
            ) AS products
          FROM catalog.group_review_candidates c
          JOIN catalog.group_review_candidate_members m ON m.candidate_id = c.id
          JOIN catalog.silver_products sp ON sp.id = m.silver_product_id
          LEFT JOIN public.products pp ON pp.silver_product_id = sp.id
          WHERE {' AND '.join(where)}
          GROUP BY c.id
          ORDER BY c.confidence DESC, c.created_at ASC
          LIMIT %s
        """
        with self.connect() as conn, conn.cursor() as cur:
            cur.execute(sql, params)
            return [
                {
                    "id": row[0],
                    "kind": row[1],
                    "status": row[2],
                    "canonical_name": row[3],
                    "confidence": row[4],
                    "reason": row[5],
                    "ai_model": row[6],
                    "ai_confidence": row[7],
                    "ai_reason": row[8],
                    "safety_flags": row[9],
                    "products": row[10] or [],
                }
                for row in cur.fetchall()
            ]

    def apply(self, candidate_id: str, action: str) -> list[tuple[str, int]]:
        if not UUID_RE.match(candidate_id):
            raise ValueError("Bad candidate id")
        if action not in {"approve", "reject", "needs_later"}:
            raise ValueError("Bad action")
        with self.connect() as conn, conn.cursor() as cur:
            cur.execute("SELECT status, rows_affected FROM catalog.apply_group_review_candidate(%s, %s)", (candidate_id, action))
            rows = cur.fetchall()
            conn.commit()
            return [(row[0], row[1]) for row in rows]

    def approve_ai_batch(self, *, kind: str, limit: int) -> dict[str, int]:
        limit = min(max(limit, 1), 100)
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
        total_products = 0
        approved = 0
        with self.connect() as conn, conn.cursor() as cur:
            cur.execute(sql, params)
            candidate_ids = [row[0] for row in cur.fetchall()]
            for candidate_id in candidate_ids:
                cur.execute(
                    "SELECT status, rows_affected FROM catalog.apply_group_review_candidate(%s, 'approve')",
                    (candidate_id,),
                )
                rows = cur.fetchall()
                approved += 1
                total_products += sum(int(row[1] or 0) for row in rows)
            conn.commit()
        return {"candidates": approved, "products": total_products}


class Handler(BaseHTTPRequestHandler):
    store: ReviewStore

    def do_GET(self) -> None:  # noqa: N802
        parsed = urlparse(self.path)
        if parsed.path != "/":
            self.send_error(HTTPStatus.NOT_FOUND)
            return
        qs = parse_qs(parsed.query)
        status = (qs.get("status") or ["pending"])[0]
        kind = (qs.get("kind") or ["all"])[0]
        try:
            limit = min(max(int((qs.get("limit") or ["50"])[0]), 1), 200)
        except ValueError:
            limit = 50
        candidates = self.store.list_candidates(status=status, kind=kind, limit=limit)
        self.respond_html(render_page(candidates, status=status, kind=kind, limit=limit))

    def do_POST(self) -> None:  # noqa: N802
        parsed = urlparse(self.path)
        if parsed.path == "/batch/approve-ai":
            self.handle_batch_approve_ai()
            return
        match = re.match(r"^/candidate/([0-9a-fA-F-]{36})/(approve|reject|needs_later)$", parsed.path)
        if not match:
            self.send_error(HTTPStatus.NOT_FOUND)
            return
        candidate_id, action = match.groups()
        try:
            result = self.store.apply(candidate_id, action)
            body = json.dumps({"ok": True, "result": result}).encode("utf-8")
            self.send_response(HTTPStatus.SEE_OTHER)
            self.send_header("Location", "/")
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        except Exception as error:  # noqa: BLE001 - operator-facing local tool
            self.send_error(HTTPStatus.INTERNAL_SERVER_ERROR, str(error))

    def handle_batch_approve_ai(self) -> None:
        try:
            length = int(self.headers.get("Content-Length") or "0")
        except ValueError:
            length = 0
        body = self.rfile.read(length).decode("utf-8") if length else ""
        form = parse_qs(body)
        kind = (form.get("kind") or ["all"])[0]
        try:
            limit = min(max(int((form.get("limit") or ["100"])[0]), 1), 100)
        except ValueError:
            limit = 100
        try:
            result = self.store.approve_ai_batch(kind=kind, limit=limit)
            redirect = f"/?status=pending&kind={kind if kind in {'exact', 'substitute'} else 'all'}&limit={limit}"
            body_bytes = json.dumps({"ok": True, "result": result}).encode("utf-8")
            self.send_response(HTTPStatus.SEE_OTHER)
            self.send_header("Location", redirect)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body_bytes)))
            self.end_headers()
            self.wfile.write(body_bytes)
        except Exception as error:  # noqa: BLE001 - operator-facing local tool
            self.send_error(HTTPStatus.INTERNAL_SERVER_ERROR, str(error))

    def log_message(self, fmt: str, *args: Any) -> None:
        sys.stderr.write("[catalog-review] " + fmt % args + "\n")

    def respond_html(self, html_text: str) -> None:
        body = html_text.encode("utf-8")
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


def render_page(candidates: list[dict[str, Any]], *, status: str, kind: str, limit: int) -> str:
    cards = "\n".join(render_candidate(candidate) for candidate in candidates)
    batch_limit = min(limit, 100)
    return f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1" />
  <title>Catalog Group Review</title>
  <style>
    body {{ font-family: system-ui, -apple-system, Segoe UI, sans-serif; margin: 0; background: #f6f5f2; color: #1d2521; }}
    header {{ position: sticky; top: 0; background: #ffffff; border-bottom: 1px solid #ddd8ce; padding: 16px 22px; z-index: 2; }}
    main {{ padding: 18px 22px 40px; }}
    form.filters {{ display: flex; gap: 10px; flex-wrap: wrap; align-items: center; }}
    .toolbar {{ display: flex; gap: 10px; flex-wrap: wrap; align-items: center; margin-top: 10px; }}
    select, input, button {{ border: 1px solid #bbb4aa; border-radius: 6px; padding: 8px 10px; background: white; font: inherit; }}
    button {{ cursor: pointer; font-weight: 700; }}
    .candidate {{ background: white; border: 1px solid #ddd8ce; border-radius: 8px; margin: 16px 0; padding: 16px; }}
    .top {{ display: flex; justify-content: space-between; gap: 16px; align-items: flex-start; }}
    .badge {{ display: inline-block; border-radius: 999px; padding: 3px 8px; background: #e8f0ea; font-size: 12px; font-weight: 700; text-transform: uppercase; }}
    .reason {{ color: #56615b; margin: 8px 0 0; max-width: 900px; }}
    .actions {{ display: flex; gap: 8px; flex-wrap: wrap; }}
    .approve {{ background: #1f7a4d; color: white; border-color: #1f7a4d; }}
    .batch {{ background: #174a63; color: white; border-color: #174a63; }}
    .reject {{ background: #9d2f2f; color: white; border-color: #9d2f2f; }}
    .later {{ background: #f2e7c9; border-color: #c5ad68; }}
    .grid {{ display: grid; grid-template-columns: repeat(auto-fill, minmax(270px, 1fr)); gap: 12px; margin-top: 14px; }}
    .product {{ border: 1px solid #ebe5dc; border-radius: 8px; padding: 10px; display: grid; grid-template-columns: 70px 1fr; gap: 10px; min-height: 92px; }}
    .product img {{ width: 70px; height: 70px; object-fit: contain; background: #fafafa; border-radius: 6px; }}
    .store {{ font-weight: 800; color: #435047; }}
    .name {{ font-weight: 700; }}
    .meta {{ color: #68736d; font-size: 13px; margin-top: 4px; }}
    pre {{ white-space: pre-wrap; background: #f7f7f7; padding: 8px; border-radius: 6px; font-size: 12px; }}
  </style>
</head>
<body>
  <header>
    <h1>Catalog Group Review</h1>
    <form class="filters" method="get">
      <label>Status <select name="status">{options(["pending","needs_later","approved","rejected"], status)}</select></label>
      <label>Kind <select name="kind">{options(["all","exact","substitute"], kind)}</select></label>
      <label>Limit <input name="limit" type="number" min="1" max="200" value="{h(limit)}" /></label>
      <button type="submit">Filter</button>
    </form>
    <div class="toolbar">
      <form method="post" action="/batch/approve-ai">
        <input type="hidden" name="kind" value="{h(kind)}" />
        <input type="hidden" name="limit" value="{h(batch_limit)}" />
        <button class="batch" type="submit">Approve next {h(batch_limit)} AI-approved</button>
      </form>
    </div>
  </header>
  <main>
    <p>{len(candidates)} candidate(s)</p>
    {cards or '<p>No candidates found.</p>'}
  </main>
</body>
</html>"""


def options(values: list[str], selected: str) -> str:
    return "".join(f'<option value="{h(value)}" {"selected" if value == selected else ""}>{h(value)}</option>' for value in values)


def render_candidate(candidate: dict[str, Any]) -> str:
    flags = candidate.get("safety_flags")
    products = "\n".join(render_product(product) for product in candidate["products"])
    ai = ""
    if candidate.get("ai_reason"):
        ai = f"""
        <p class="reason"><strong>AI:</strong> {h(candidate.get("ai_reason"))}
        {f"({candidate.get('ai_confidence'):.2f})" if candidate.get('ai_confidence') is not None else ""}</p>
        {f"<pre>{h(json.dumps(flags, indent=2, ensure_ascii=False))}</pre>" if flags else ""}
        """
    return f"""
    <section class="candidate">
      <div class="top">
        <div>
          <span class="badge">{h(candidate['kind'])}</span>
          <span class="badge">{h(candidate['status'])}</span>
          <h2>{h(candidate.get('canonical_name'))}</h2>
          <p class="reason">{h(candidate.get('reason'))} ({candidate.get('confidence'):.2f})</p>
          {ai}
        </div>
        <div class="actions">
          <form method="post" action="/candidate/{h(candidate['id'])}/approve"><button class="approve">Approve</button></form>
          <form method="post" action="/candidate/{h(candidate['id'])}/reject"><button class="reject">Reject</button></form>
          <form method="post" action="/candidate/{h(candidate['id'])}/needs_later"><button class="later">Needs later</button></form>
        </div>
      </div>
      <div class="grid">{products}</div>
    </section>
    """


def render_product(product: dict[str, Any]) -> str:
    image = product.get("image_url")
    img = f'<img src="{h(image)}" alt="" loading="lazy" />' if image else '<div></div>'
    base = money(product.get("base_price"))
    unit = product.get("base_price_unit") or "-"
    return f"""
      <article class="product">
        {img}
        <div>
          <div class="store">{h(product.get('store'))}</div>
          <div class="name">{h(product.get('name'))}</div>
          <div class="meta">EAN: {h(product.get('ean') or '-')}</div>
          <div class="meta">Silver: {h(product.get('silver_product_id'))}</div>
          <div class="meta">Price: {money(product.get('price'))} · Base: {base}/{h(unit)}</div>
          <div class="meta">Public: {cents(product.get('current_price_cents'))}</div>
        </div>
      </article>
    """


def main() -> int:
    parser = argparse.ArgumentParser(description="Run local catalog grouping review UI.")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8094)
    args = parser.parse_args()

    Handler.store = ReviewStore()
    server = ThreadingHTTPServer((args.host, args.port), Handler)
    print(f"Catalog review server listening on http://{args.host}:{args.port}")
    server.serve_forever()
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        sys.exit(130)
