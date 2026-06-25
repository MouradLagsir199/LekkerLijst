"""Shared utilities for the supermarket bronze scrapers.

Every store scraper enumerates a full catalog and writes one raw product object
per line to a JSONL file (the bronze staging artifact). ``bronze_ingest`` then
loads that JSONL into ``catalog.bronze_products``.

Design rules:
- Scrapers stay dumb: they only fetch and dump raw API/HTML payloads. No
  normalization happens here (that is the later silver step).
- One JSONL line == one raw product as the store exposes it, optionally merged
  from a list + detail call. The line MUST be valid JSON on a single line.
- The bronze row hash is content-addressed (store + canonical payload), so a
  re-run of an unchanged catalog inserts nothing new.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

REPO_ROOT = Path(__file__).resolve().parents[1]
OUTPUT_DIR = REPO_ROOT / "Output"

RETRYABLE_STATUS = {429, 500, 502, 503, 504}


def now_iso() -> str:
    """UTC timestamp in ISO-8601, used as the scraped_at on every record."""
    return datetime.now(timezone.utc).isoformat()


def canonical_json(obj: Any) -> str:
    """Deterministic JSON: sorted keys, compact separators, UTF-8 preserved."""
    return json.dumps(obj, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def compute_row_hash(store: str, payload: Any) -> str:
    """Content address for a bronze row. Stable for identical payloads."""
    digest = hashlib.sha256(f"{store}:{canonical_json(payload)}".encode("utf-8"))
    return digest.hexdigest()


def should_retry(status: int) -> bool:
    return status in RETRYABLE_STATUS


async def backoff_sleep(attempt: int, *, base: float = 1.5, mult: float = 1.8) -> None:
    """Exponential backoff: base, base*mult, base*mult^2, ... (attempt is 1-based)."""
    await asyncio.sleep(base * (mult ** (attempt - 1)))


class JsonlWriter:
    """Append raw product objects to a JSONL file, flushing periodically.

    Used as a context manager. ``write`` accepts any JSON-serializable object;
    the object is expected to already carry whatever envelope the scraper wants
    (store, scraped_at, the raw payload, etc.).
    """

    def __init__(self, path: Path, *, flush_every: int = 200) -> None:
        self.path = Path(path)
        self.flush_every = flush_every
        self.count = 0
        self._handle = None

    def __enter__(self) -> "JsonlWriter":
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._handle = self.path.open("w", encoding="utf-8")
        return self

    def write(self, obj: Any) -> None:
        assert self._handle is not None, "JsonlWriter used outside context manager"
        self._handle.write(json.dumps(obj, ensure_ascii=False) + "\n")
        self.count += 1
        if self.count % self.flush_every == 0:
            self._handle.flush()

    def write_many(self, objs: Iterable[Any]) -> None:
        for obj in objs:
            self.write(obj)

    def __exit__(self, *exc: object) -> None:
        if self._handle is not None:
            self._handle.flush()
            self._handle.close()
            self._handle = None


def read_jsonl(path: Path) -> Iterable[dict[str, Any]]:
    """Yield each JSON object from a JSONL file, skipping blank lines."""
    with Path(path).open("r", encoding="utf-8") as handle:
        for line in handle:
            line = line.strip()
            if line:
                yield json.loads(line)


def default_output_path(store: str) -> Path:
    return OUTPUT_DIR / f"{store}_bronze.jsonl"


def env(name: str, default: str | None = None) -> str | None:
    return os.environ.get(name, default)
