"""Phase 1 of cross-store substitute grouping: LLM semantic canonical keys.

The deterministic baseline (catalog.refresh_canonical_baseline) groups products by
KIND but fragments on brand + inflection ("biologisch komkommer" vs "biologische
komkommer"; "arla halfvolle melk" vs "campina halfvolle melk"). This script sends
every distinct product name to the OpenAI API and overwrites the rule key with a
semantic canonical_key + clean Dutch display_name + category, so the same kind of
product groups across all stores regardless of brand or spelling.

Provider/keys (matches the recipe-import edge function):
  OPENAI_API_KEY        required
  OPENAI_CATALOG_MODEL  default gpt-5.4-mini
  SUPABASE_DB_URL       pooler DSN (psycopg)

Two transports:
  * pilot  — synchronous /v1/responses calls (threaded), for a small validation
             batch. Fast feedback, no 24h wait. Use this to vet the prompt.
  * build/submit/status/download/load — the OpenAI Batch API (50% cheaper, 24h
             window) for the full ~79k-name run.

Typical full run:
  python -m scrapers.canonical_tag build              # -> Output/canonical_requests.jsonl (+ .map.json)
  python -m scrapers.canonical_tag submit             # -> prints batch id
  python -m scrapers.canonical_tag status  <batch_id> # poll until completed
  python -m scrapers.canonical_tag download <batch_id># -> Output/canonical_output.jsonl
  python -m scrapers.canonical_tag load               # parse output -> catalog.name_canonical

Pilot run (validate the prompt on tricky cases, no Batch API):
  python -m scrapers.canonical_tag pilot --limit 300
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from datetime import datetime, timezone
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

import httpx
import psycopg
from psycopg.rows import dict_row

OPENAI_BASE = "https://api.openai.com/v1"
OUTPUT_DIR = Path(__file__).resolve().parents[1] / "Output"
REQUESTS_PATH = OUTPUT_DIR / "canonical_requests.jsonl"
MAP_PATH = OUTPUT_DIR / "canonical_requests.map.json"
RESULTS_PATH = OUTPUT_DIR / "canonical_output.jsonl"
CHUNKS_DIR = OUTPUT_DIR / "canonical_batches"
MANIFEST_PATH = CHUNKS_DIR / "manifest.json"

CATEGORIES = [
    "groente", "fruit", "aardappel", "zuivel", "kaas", "eieren", "vlees", "vis",
    "vleeswaren", "brood", "banket", "ontbijtgranen", "frisdrank", "sappen",
    "water", "warme_dranken", "alcohol", "pasta_rijst", "conserven", "sauzen",
    "kruiden_specerijen", "olie_azijn", "bakproducten", "noten_zuidvruchten",
    "snoep", "koek", "chips_snacks", "diepvries", "kant_en_klaar", "baby",
    "huishouden", "persoonlijke_verzorging", "huisdier", "non_food", "overig",
]

SYSTEM_PROMPT = (
    "Je bent een data-normalisator voor Nederlandse supermarkten. Je krijgt één "
    "productnaam en bepaalt een CANONIEKE GROEPSSLEUTEL zodat dezelfde SOORT product "
    "over verschillende supermarkten heen vergeleken kan worden.\n\n"
    "REGELS voor canonical_key:\n"
    "- lowercase snake_case Nederlandse slug van de product-SOORT.\n"
    "- LAAT WEG: merknaam (Calvé, Campina, Arla, AH, Jumbo, PLUS, huismerk), "
    "verpakkingsmaat/aantal (500g, 1l, 6-pack), winkelnaam en woorden als 'vers' of 'voordeel'.\n"
    "- Producten die een recept als ONDERLING VERVANGBAAR zou behandelen krijgen DEZELFDE sleutel. "
    "Verschillende merken van hetzelfde product = zelfde sleutel.\n"
    "- BEHOUD eigenschappen die het product/de prijs wezenlijk bepalen:\n"
    "    vetgehalte:  halfvolle_melk ≠ volle_melk ≠ magere_melk\n"
    "    smaak:       cola ≠ cola_zero ; fanta_orange ≠ fanta_cassis\n"
    "    bereiding:   komkommer ≠ komkommersalade ; tomaat ≠ tomatenblokjes\n"
    "    soort:       kipfilet ≠ kipgehakt ; rundergehakt ≠ half_om_half_gehakt\n"
    "- NEGEER biologisch/bio in de sleutel zelf; zet dat in is_organic. "
    "Dus 'biologische komkommer' en 'komkommer' krijgen BEIDE de sleutel 'komkommer'.\n"
    "- Spellings-/buigingsvarianten van hetzelfde woord normaliseren naar één vorm.\n\n"
    "display_name: nette Nederlandse weergavenaam van de soort (zonder merk/maat), bv. 'Komkommer', 'Halfvolle melk'.\n"
    "category: kies precies één waarde uit de toegestane lijst.\n"
    "unit_type: 'piece' (per stuk verkocht), 'weight' (per gewicht: vlees, kaas, los fruit) of 'volume' (vloeistof).\n\n"
    "Voorbeelden:\n"
    "  'AH Komkommer'            -> {\"canonical_key\":\"komkommer\",\"display_name\":\"Komkommer\",\"category\":\"groente\",\"is_organic\":false,\"unit_type\":\"piece\"}\n"
    "  'Biologische komkommer'   -> {\"canonical_key\":\"komkommer\",\"display_name\":\"Komkommer\",\"category\":\"groente\",\"is_organic\":true,\"unit_type\":\"piece\"}\n"
    "  'Jumbo Snoepkomkommer'    -> {\"canonical_key\":\"snoepkomkommer\",\"display_name\":\"Snoepkomkommer\",\"category\":\"groente\",\"is_organic\":false,\"unit_type\":\"piece\"}\n"
    "  'Campina Halfvolle melk 1L'-> {\"canonical_key\":\"halfvolle_melk\",\"display_name\":\"Halfvolle melk\",\"category\":\"zuivel\",\"is_organic\":false,\"unit_type\":\"volume\"}\n"
    "  'Arla Biologisch volle melk'-> {\"canonical_key\":\"volle_melk\",\"display_name\":\"Volle melk\",\"category\":\"zuivel\",\"is_organic\":true,\"unit_type\":\"volume\"}\n"
    "  'Coca-Cola Zero 1,5L'     -> {\"canonical_key\":\"cola_zero\",\"display_name\":\"Cola zero\",\"category\":\"frisdrank\",\"is_organic\":false,\"unit_type\":\"volume\"}\n"
    "  'Calvé Pindakaas 350g'    -> {\"canonical_key\":\"pindakaas\",\"display_name\":\"Pindakaas\",\"category\":\"sauzen\",\"is_organic\":false,\"unit_type\":\"weight\"}\n"
    "  'Heinz Sandwich spread komkommer' -> {\"canonical_key\":\"sandwich_spread\",\"display_name\":\"Sandwich spread\",\"category\":\"sauzen\",\"is_organic\":false,\"unit_type\":\"weight\"}\n"
)

TAG_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "required": ["canonical_key", "display_name", "category", "is_organic", "unit_type"],
    "properties": {
        "canonical_key": {"type": "string", "minLength": 1, "maxLength": 60},
        "display_name": {"type": "string", "minLength": 1, "maxLength": 60},
        "category": {"type": "string", "enum": CATEGORIES},
        "is_organic": {"type": "boolean"},
        "unit_type": {"type": "string", "enum": ["piece", "weight", "volume"]},
    },
}


def model_name() -> str:
    return os.environ.get("OPENAI_CATALOG_MODEL", "gpt-5.4-mini")


def dsn() -> str:
    d = os.environ.get("SUPABASE_DB_URL")
    if not d:
        sys.exit("Set SUPABASE_DB_URL (pooler DSN).")
    return d


def api_key() -> str:
    k = os.environ.get("OPENAI_API_KEY")
    if not k:
        sys.exit("Set OPENAI_API_KEY (the same key used by the recipe-import edge function).")
    return k


def request_body(sample_name: str) -> dict:
    return {
        "model": model_name(),
        "input": [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": sample_name},
        ],
        "text": {
            "format": {
                "type": "json_schema",
                "name": "product_tag",
                "strict": True,
                "schema": TAG_SCHEMA,
            }
        },
    }


def extract_output_text(resp: dict) -> str | None:
    """Pull the model's text out of a /v1/responses payload (handles both shapes)."""
    if isinstance(resp.get("output_text"), str) and resp["output_text"].strip():
        return resp["output_text"]
    for item in resp.get("output", []) or []:
        for chunk in item.get("content", []) or []:
            if chunk.get("type") in ("output_text", "text") and chunk.get("text"):
                return chunk["text"]
    return None


def fetch_names_to_tag(conn, limit: int | None, only_untagged: bool, like: str | None):
    """Distinct product names needing a semantic tag, with a readable sample name."""
    where = ["p.is_available = true", "p.name_search IS NOT NULL", "p.name_search <> ''"]
    params: list = []
    if only_untagged:
        where.append("(nc.source IS NULL OR nc.source = 'rule')")
    if like:
        where.append("p.name_search LIKE %s")
        params.append(f"%{like}%")
    sql = f"""
        SELECT p.name_search, min(p.name) AS sample_name
        FROM public.products p
        LEFT JOIN catalog.name_canonical nc ON nc.name_search = p.name_search
        WHERE {' AND '.join(where)}
        GROUP BY p.name_search
        ORDER BY count(*) DESC, p.name_search
    """
    if limit:
        sql += f" LIMIT {int(limit)}"
    return conn.execute(sql, params).fetchall()


# ── build / submit / status / download / load (Batch API) ─────────────────────

def cmd_build(args):
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    with psycopg.connect(dsn(), prepare_threshold=None, row_factory=dict_row) as conn:
        rows = fetch_names_to_tag(conn, args.limit, not args.all, args.like)
    if args.chunks:
        write_chunked_requests(rows, args.chunk_requests, args.chunk_bytes)
        return
    id_map: dict[str, str] = {}
    with REQUESTS_PATH.open("w", encoding="utf-8") as fh:
        for i, r in enumerate(rows):
            cid = f"n{i}"
            id_map[cid] = r["name_search"]
            fh.write(json.dumps({
                "custom_id": cid,
                "method": "POST",
                "url": "/v1/responses",
                "body": request_body(r["sample_name"]),
            }, ensure_ascii=False) + "\n")
    MAP_PATH.write_text(json.dumps(id_map, ensure_ascii=False), encoding="utf-8")
    print(f"wrote {len(rows)} requests -> {REQUESTS_PATH}")
    print(f"id map -> {MAP_PATH}")


def write_chunked_requests(rows, max_requests: int, max_bytes: int) -> None:
    CHUNKS_DIR.mkdir(parents=True, exist_ok=True)
    for old_path in CHUNKS_DIR.glob("canonical_*part*.jsonl"):
        old_path.unlink()
    for old_path in CHUNKS_DIR.glob("canonical_requests.part*.map.json"):
        old_path.unlink()
    if MANIFEST_PATH.exists():
        MANIFEST_PATH.unlink()

    manifest: list[dict] = []
    chunk_lines: list[str] = []
    chunk_map: dict[str, str] = {}
    chunk_bytes = 0
    chunk_index = 1

    def flush() -> None:
        nonlocal chunk_lines, chunk_map, chunk_bytes, chunk_index
        if not chunk_lines:
            return
        stem = f"part{chunk_index:03d}"
        request_path = CHUNKS_DIR / f"canonical_requests.{stem}.jsonl"
        map_path = CHUNKS_DIR / f"canonical_requests.{stem}.map.json"
        request_path.write_text("".join(chunk_lines), encoding="utf-8")
        map_path.write_text(json.dumps(chunk_map, ensure_ascii=False), encoding="utf-8")
        manifest.append({
            "part": stem,
            "request_file": request_path.name,
            "map_file": map_path.name,
            "output_file": f"canonical_output.{stem}.jsonl",
            "requests": len(chunk_lines),
            "bytes": request_path.stat().st_size,
        })
        print(f"wrote {len(chunk_lines)} requests ({request_path.stat().st_size} bytes) -> {request_path}")
        chunk_lines = []
        chunk_map = {}
        chunk_bytes = 0
        chunk_index += 1

    for i, r in enumerate(rows):
        cid = f"n{i}"
        line = json.dumps({
            "custom_id": cid,
            "method": "POST",
            "url": "/v1/responses",
            "body": request_body(r["sample_name"]),
        }, ensure_ascii=False) + "\n"
        line_bytes = len(line.encode("utf-8"))
        if chunk_lines and (len(chunk_lines) >= max_requests or chunk_bytes + line_bytes > max_bytes):
            flush()
        chunk_lines.append(line)
        chunk_map[cid] = r["name_search"]
        chunk_bytes += line_bytes
    flush()
    MANIFEST_PATH.write_text(json.dumps(manifest, indent=2, ensure_ascii=False), encoding="utf-8")
    print(f"manifest -> {MANIFEST_PATH}")


def load_manifest() -> list[dict]:
    if not MANIFEST_PATH.exists():
        sys.exit(f"{MANIFEST_PATH} not found — run `build --chunks` first.")
    return json.loads(MANIFEST_PATH.read_text(encoding="utf-8"))


def save_manifest(manifest: list[dict]) -> None:
    CHUNKS_DIR.mkdir(parents=True, exist_ok=True)
    MANIFEST_PATH.write_text(json.dumps(manifest, indent=2, ensure_ascii=False), encoding="utf-8")


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def cmd_submit_chunks(args):
    key = api_key()
    manifest = load_manifest()
    with httpx.Client(timeout=180) as client:
        for item in manifest:
            if item.get("batch_id") and not args.resubmit:
                print(f"{item['part']}: already submitted as {item['batch_id']}")
                continue
            request_path = CHUNKS_DIR / item["request_file"]
            if not request_path.exists():
                sys.exit(f"{request_path} not found")
            files = {"file": (request_path.name, request_path.read_bytes(), "application/jsonl")}
            up = client.post(
                f"{OPENAI_BASE}/files",
                headers={"Authorization": f"Bearer {key}"},
                data={"purpose": "batch"},
                files=files,
            )
            up.raise_for_status()
            file_id = up.json()["id"]
            batch = client.post(
                f"{OPENAI_BASE}/batches",
                headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
                json={"input_file_id": file_id, "endpoint": "/v1/responses", "completion_window": "24h"},
            )
            batch.raise_for_status()
            b = batch.json()
            item.update({
                "input_file_id": file_id,
                "batch_id": b["id"],
                "status": b["status"],
                "submitted_at": now_iso(),
            })
            save_manifest(manifest)
            print(f"{item['part']}: uploaded {file_id}; batch {b['id']} status={b['status']}")
    print(f"updated manifest -> {MANIFEST_PATH}")


def cmd_status_chunks(args):
    key = api_key()
    manifest = load_manifest()
    with httpx.Client(timeout=60) as client:
        for item in manifest:
            batch_id = item.get("batch_id")
            if not batch_id:
                print(f"{item['part']}: not submitted")
                continue
            r = client.get(f"{OPENAI_BASE}/batches/{batch_id}", headers={"Authorization": f"Bearer {key}"})
            r.raise_for_status()
            b = r.json()
            item["status"] = b["status"]
            item["request_counts"] = b.get("request_counts")
            item["output_file_id"] = b.get("output_file_id")
            item["error_file_id"] = b.get("error_file_id")
            item["errors"] = b.get("errors")
            counts = b.get("request_counts") or {}
            print(
                f"{item['part']}: {batch_id} status={b['status']} "
                f"completed={counts.get('completed')}/{counts.get('total')} "
                f"failed={counts.get('failed')} output={b.get('output_file_id')}"
            )
            print_batch_errors(b, prefix=f"{item['part']}: ")
    save_manifest(manifest)


def download_and_load_chunks(*, require_complete: bool) -> int:
    key = api_key()
    manifest = load_manifest()
    loaded_total = 0
    with httpx.Client(timeout=180) as client:
        for item in manifest:
            batch_id = item.get("batch_id")
            if not batch_id:
                if require_complete:
                    sys.exit(f"{item['part']}: not submitted")
                continue
            r = client.get(f"{OPENAI_BASE}/batches/{batch_id}", headers={"Authorization": f"Bearer {key}"})
            r.raise_for_status()
            b = r.json()
            item["status"] = b["status"]
            item["request_counts"] = b.get("request_counts")
            item["output_file_id"] = b.get("output_file_id")
            item["error_file_id"] = b.get("error_file_id")
            item["errors"] = b.get("errors")
            if b["status"] != "completed":
                if require_complete:
                    sys.exit(f"{item['part']}: batch {batch_id} is {b['status']}, not completed")
                print(f"{item['part']}: skipping status={b['status']}")
                continue
            out_id = b.get("output_file_id")
            if not out_id:
                sys.exit(f"{item['part']}: completed batch has no output_file_id")
            output_path = CHUNKS_DIR / item["output_file"]
            content = client.get(f"{OPENAI_BASE}/files/{out_id}/content", headers={"Authorization": f"Bearer {key}"})
            content.raise_for_status()
            output_path.write_bytes(content.content)
            print(f"{item['part']}: downloaded -> {output_path}")

            map_path = CHUNKS_DIR / item["map_file"]
            id_map = json.loads(map_path.read_text(encoding="utf-8"))
            with psycopg.connect(dsn(), prepare_threshold=None, autocommit=True) as conn:
                loaded = upsert_tags(conn, parse_batch_output(output_path, id_map), model_name())
            item["loaded"] = loaded
            item["loaded_at"] = now_iso()
            loaded_total += loaded
            save_manifest(manifest)
            print(f"{item['part']}: loaded {loaded} semantic tags")
    save_manifest(manifest)
    return loaded_total


def cmd_download_load_chunks(args):
    loaded = download_and_load_chunks(require_complete=not args.skip_incomplete)
    print(f"loaded {loaded} semantic tags across chunks")


def cmd_wait_chunks(args):
    while True:
        manifest = load_manifest()
        statuses = {item.get("status") for item in manifest}
        if statuses and statuses <= TERMINAL:
            break
        cmd_status_chunks(args)
        manifest = load_manifest()
        statuses = {item.get("status") for item in manifest}
        if statuses and statuses <= TERMINAL:
            break
        time.sleep(args.interval)

    manifest = load_manifest()
    bad = [item for item in manifest if item.get("status") != "completed"]
    if bad:
        for item in bad:
            print(f"{item['part']}: terminal non-completed status={item.get('status')} error={item.get('error_file_id')}")
        sys.exit("one or more chunks did not complete")
    loaded = download_and_load_chunks(require_complete=True)
    print(f"loaded {loaded} semantic tags across chunks")


def cmd_submit(args):
    key = api_key()
    if not REQUESTS_PATH.exists():
        sys.exit(f"{REQUESTS_PATH} not found — run `build` first.")
    with httpx.Client(timeout=120) as client:
        files = {"file": (REQUESTS_PATH.name, REQUESTS_PATH.read_bytes(), "application/jsonl")}
        up = client.post(f"{OPENAI_BASE}/files", headers={"Authorization": f"Bearer {key}"},
                         data={"purpose": "batch"}, files=files)
        up.raise_for_status()
        file_id = up.json()["id"]
        print(f"uploaded input file: {file_id}")
        batch = client.post(f"{OPENAI_BASE}/batches",
                            headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
                            json={"input_file_id": file_id, "endpoint": "/v1/responses",
                                  "completion_window": "24h"})
        batch.raise_for_status()
        b = batch.json()
        print(f"batch id: {b['id']}   status: {b['status']}")


def cmd_status(args):
    key = api_key()
    with httpx.Client(timeout=60) as client:
        r = client.get(f"{OPENAI_BASE}/batches/{args.batch_id}",
                       headers={"Authorization": f"Bearer {key}"})
        r.raise_for_status()
        b = r.json()
        print(f"status: {b['status']}   counts: {b.get('request_counts')}   output_file_id: {b.get('output_file_id')}")
        print_batch_errors(b)


def print_batch_errors(batch: dict, prefix: str = "") -> None:
    errors = batch.get("errors") or {}
    data = errors.get("data") if isinstance(errors, dict) else None
    if not data:
        return
    print(f"{prefix}validation errors:")
    for err in data[:20]:
        line = err.get("line")
        param = err.get("param")
        code = err.get("code")
        message = err.get("message")
        location = []
        if line is not None:
            location.append(f"line={line}")
        if param:
            location.append(f"param={param}")
        where = f" ({', '.join(location)})" if location else ""
        print(f"{prefix}  - {code or 'error'}{where}: {message}")
    if len(data) > 20:
        print(f"{prefix}  ... {len(data) - 20} more validation errors")


def cmd_download(args):
    key = api_key()
    with httpx.Client(timeout=120) as client:
        r = client.get(f"{OPENAI_BASE}/batches/{args.batch_id}",
                       headers={"Authorization": f"Bearer {key}"})
        r.raise_for_status()
        out_id = r.json().get("output_file_id")
        if not out_id:
            sys.exit(f"batch {args.batch_id} has no output_file_id yet (status {r.json().get('status')}).")
        content = client.get(f"{OPENAI_BASE}/files/{out_id}/content",
                            headers={"Authorization": f"Bearer {key}"})
        content.raise_for_status()
        RESULTS_PATH.write_bytes(content.content)
    print(f"downloaded -> {RESULTS_PATH}")


def parse_batch_output(path: Path, id_map: dict[str, str]):
    """Yield (name_search, tag_dict) from a Batch API output JSONL."""
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        rec = json.loads(line)
        cid = rec.get("custom_id")
        name_search = id_map.get(cid)
        if not name_search:
            continue
        body = (rec.get("response") or {}).get("body") or {}
        text = extract_output_text(body)
        if not text:
            continue
        try:
            yield name_search, json.loads(text)
        except json.JSONDecodeError:
            continue


def upsert_tags(conn, items, model: str, source: str = "ai_batch") -> int:
    n = 0
    for name_search, tag in items:
        key = (tag.get("canonical_key") or "").strip().lower()
        if not key:
            continue
        conn.execute(
            """
            INSERT INTO catalog.name_canonical
              (name_search, canonical_key, display_name, category, is_organic, unit_type,
               confidence, source, model, tagged_at)
            VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s, now())
            ON CONFLICT (name_search) DO UPDATE SET
              canonical_key = EXCLUDED.canonical_key,
              display_name  = EXCLUDED.display_name,
              category      = EXCLUDED.category,
              is_organic    = EXCLUDED.is_organic,
              unit_type     = EXCLUDED.unit_type,
              confidence    = EXCLUDED.confidence,
              source        = EXCLUDED.source,
              model         = EXCLUDED.model,
              tagged_at     = now()
            """,
            (name_search, key, tag.get("display_name"), tag.get("category"),
             tag.get("is_organic"), tag.get("unit_type"), 0.9, source, model),
        )
        n += 1
    return n


def cmd_load(args):
    if not RESULTS_PATH.exists():
        sys.exit(f"{RESULTS_PATH} not found — run `download` first.")
    if not MAP_PATH.exists():
        sys.exit(f"{MAP_PATH} not found — it is written by `build`.")
    id_map = json.loads(MAP_PATH.read_text(encoding="utf-8"))
    with psycopg.connect(dsn(), prepare_threshold=None, autocommit=True) as conn:
        n = upsert_tags(conn, parse_batch_output(RESULTS_PATH, id_map), model_name())
    print(f"loaded {n} semantic tags into catalog.name_canonical")


TERMINAL = {"completed", "failed", "expired", "cancelled"}


def cmd_wait(args):
    """Poll a batch to terminal state; on completion auto-download + auto-load."""
    key = api_key()
    with httpx.Client(timeout=120) as client:
        headers = {"Authorization": f"Bearer {key}"}
        while True:
            r = client.get(f"{OPENAI_BASE}/batches/{args.batch_id}", headers=headers)
            r.raise_for_status()
            b = r.json()
            status = b["status"]
            counts = b.get("request_counts") or {}
            print(f"[{status}] completed={counts.get('completed')}/{counts.get('total')} "
                  f"failed={counts.get('failed')}", flush=True)
            if status in TERMINAL:
                break
            time.sleep(args.interval)

    if status != "completed":
        sys.exit(f"batch ended in non-completed state: {status} (error_file_id={b.get('error_file_id')})")

    out_id = b.get("output_file_id")
    if not out_id:
        sys.exit("completed batch has no output_file_id")
    with httpx.Client(timeout=180) as client:
        content = client.get(f"{OPENAI_BASE}/files/{out_id}/content",
                             headers={"Authorization": f"Bearer {key}"})
        content.raise_for_status()
        RESULTS_PATH.write_bytes(content.content)
    print(f"downloaded -> {RESULTS_PATH}")

    id_map = json.loads(MAP_PATH.read_text(encoding="utf-8"))
    with psycopg.connect(dsn(), prepare_threshold=None, autocommit=True) as conn:
        n = upsert_tags(conn, parse_batch_output(RESULTS_PATH, id_map), model_name())
    print(f"loaded {n} semantic tags into catalog.name_canonical")


# ── pilot (synchronous, for prompt validation) ────────────────────────────────

def tag_one_sync(client: httpx.Client, key: str, sample_name: str) -> dict | None:
    r = client.post(f"{OPENAI_BASE}/responses",
                    headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
                    json=request_body(sample_name))
    if r.status_code != 200:
        return None
    text = extract_output_text(r.json())
    if not text:
        return None
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        return None


def cmd_pilot(args):
    key = api_key()
    with psycopg.connect(dsn(), prepare_threshold=None, row_factory=dict_row) as conn:
        rows = fetch_names_to_tag(conn, args.limit, only_untagged=False, like=args.like)
    print(f"pilot: tagging {len(rows)} names with {model_name()} (synchronous)…")
    results: list[tuple[str, str, dict]] = []
    with httpx.Client(timeout=60) as client:
        with ThreadPoolExecutor(max_workers=args.workers) as pool:
            futs = {pool.submit(tag_one_sync, client, key, r["sample_name"]): r for r in rows}
            for fut in as_completed(futs):
                r = futs[fut]
                tag = fut.result()
                if tag:
                    results.append((r["name_search"], r["sample_name"], tag))
    print(f"got {len(results)} tags back\n")
    # Show grouping: which sample names collapsed to the same key
    groups: dict[str, list[str]] = {}
    for _, sample, tag in results:
        groups.setdefault(tag.get("canonical_key", "?"), []).append(sample)
    for key_, names in sorted(groups.items(), key=lambda kv: -len(kv[1]))[:40]:
        print(f"  {key_:<24} <- {sorted(set(names))[:6]}")
    if args.write:
        with psycopg.connect(dsn(), prepare_threshold=None, autocommit=True) as conn:
            n = upsert_tags(conn, [(ns, t) for ns, _, t in results], model_name(), source="ai_batch")
        print(f"\nwrote {n} pilot tags into catalog.name_canonical (source=ai_batch)")


def main():
    ap = argparse.ArgumentParser(description="LLM semantic canonical tagging for catalog grouping.")
    sub = ap.add_subparsers(dest="cmd", required=True)

    b = sub.add_parser("build", help="write Batch API requests JSONL")
    b.add_argument("--limit", type=int, default=None)
    b.add_argument("--all", action="store_true", help="include already-tagged names (default: only rule/untagged)")
    b.add_argument("--like", type=str, default=None, help="only names whose name_search LIKE %%X%%")
    b.add_argument("--chunks", action="store_true", help="split into OpenAI Batch-size-safe chunk files")
    b.add_argument("--chunk-requests", type=int, default=45000, help="maximum requests per chunk")
    b.add_argument("--chunk-bytes", type=int, default=180_000_000, help="maximum bytes per chunk")
    b.set_defaults(func=cmd_build)

    s = sub.add_parser("submit", help="upload + create the batch")
    s.set_defaults(func=cmd_submit)

    sc = sub.add_parser("submit-chunks", help="upload + create every chunked batch")
    sc.add_argument("--resubmit", action="store_true", help="submit chunks even if manifest already has batch IDs")
    sc.set_defaults(func=cmd_submit_chunks)

    st = sub.add_parser("status", help="poll a batch")
    st.add_argument("batch_id")
    st.set_defaults(func=cmd_status)

    stc = sub.add_parser("status-chunks", help="poll every chunked batch in the manifest")
    stc.set_defaults(func=cmd_status_chunks)

    d = sub.add_parser("download", help="download a finished batch output")
    d.add_argument("batch_id")
    d.set_defaults(func=cmd_download)

    l = sub.add_parser("load", help="parse downloaded output into catalog.name_canonical")
    l.set_defaults(func=cmd_load)

    dlc = sub.add_parser("download-load-chunks", help="download completed chunk outputs and load tags")
    dlc.add_argument("--skip-incomplete", action="store_true", help="load completed chunks and skip unfinished chunks")
    dlc.set_defaults(func=cmd_download_load_chunks)

    w = sub.add_parser("wait", help="poll a batch until done, then auto-download + load")
    w.add_argument("batch_id")
    w.add_argument("--interval", type=int, default=60)
    w.set_defaults(func=cmd_wait)

    wc = sub.add_parser("wait-chunks", help="poll all chunks until done, then auto-download + load")
    wc.add_argument("--interval", type=int, default=60)
    wc.set_defaults(func=cmd_wait_chunks)

    p = sub.add_parser("pilot", help="synchronous tag a small sample to validate the prompt")
    p.add_argument("--limit", type=int, default=200)
    p.add_argument("--like", type=str, default=None)
    p.add_argument("--workers", type=int, default=8)
    p.add_argument("--write", action="store_true", help="persist pilot tags to the DB")
    p.set_defaults(func=cmd_pilot)

    args = ap.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
