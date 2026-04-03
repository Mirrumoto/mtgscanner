"""
mtgjson_prices.py — Local MTGJSON price cache + lookup.

Implements:
- Daily refresh of AllPricesToday.json.gz
- SQLite index for provider/side/finish lookups
- On-demand set metadata cache for set+collector_number -> MTGJSON UUID mapping
"""

from __future__ import annotations

import gzip
import json
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

import requests

ALL_PRICES_TODAY_URL = "https://mtgjson.com/api/v5/AllPricesToday.json.gz"
SET_FILE_URL_TEMPLATE = "https://mtgjson.com/api/v5/{set_code}.json"

SUPPORTED_FINISHES = {"normal", "foil", "etched"}
DEFAULT_TIMEOUT = 45


@dataclass(frozen=True)
class PricePolicy:
    provider: str = "tcgplayer"
    side: str = "retail"  # retail | buylist


class MTGJSONPriceIndex:
    def __init__(self, app_data_dir: Path):
        self.base_dir = app_data_dir / "mtgjson"
        self.base_dir.mkdir(parents=True, exist_ok=True)
        self.prices_gz_path = self.base_dir / "AllPricesToday.json.gz"
        self.meta_path = self.base_dir / "cache_meta.json"
        self.db_path = self.base_dir / "prices.db"

        self._init_db()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(str(self.db_path))
        conn.execute("PRAGMA journal_mode=WAL;")
        conn.execute("PRAGMA synchronous=NORMAL;")
        return conn

    def _init_db(self) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS price_index (
                    uuid TEXT NOT NULL,
                    provider TEXT NOT NULL,
                    side TEXT NOT NULL,
                    finish TEXT NOT NULL,
                    price REAL NOT NULL,
                    updated_at TEXT NOT NULL,
                    PRIMARY KEY (uuid, provider, side, finish)
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS card_map (
                    set_code TEXT NOT NULL,
                    collector_number TEXT NOT NULL,
                    mtgjson_uuid TEXT NOT NULL,
                    scryfall_id TEXT,
                    card_name TEXT,
                    finishes_json TEXT,
                    fetched_at TEXT NOT NULL,
                    PRIMARY KEY (set_code, collector_number, mtgjson_uuid)
                )
                """
            )
            conn.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_card_map_set_num
                ON card_map (set_code, collector_number)
                """
            )
            conn.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_card_map_scryfall
                ON card_map (scryfall_id)
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS set_cache (
                    set_code TEXT PRIMARY KEY,
                    fetched_at TEXT NOT NULL
                )
                """
            )

    def _utc_now_iso(self) -> str:
        return datetime.now(timezone.utc).isoformat()

    def _load_meta(self) -> dict:
        if not self.meta_path.exists():
            return {}
        try:
            with open(self.meta_path, "r", encoding="utf-8") as handle:
                data = json.load(handle)
                if isinstance(data, dict):
                    return data
        except (OSError, json.JSONDecodeError):
            return {}
        return {}

    def _save_meta(self, meta: dict) -> None:
        with open(self.meta_path, "w", encoding="utf-8") as handle:
            json.dump(meta, handle, indent=2, ensure_ascii=False)

    def _is_same_utc_day(self, iso_timestamp: str | None) -> bool:
        if not iso_timestamp:
            return False
        try:
            then = datetime.fromisoformat(iso_timestamp)
            if then.tzinfo is None:
                then = then.replace(tzinfo=timezone.utc)
            now = datetime.now(timezone.utc)
            return then.date() == now.date()
        except ValueError:
            return False

    def ensure_daily_prices_ready(self) -> None:
        """Download and index AllPricesToday once per UTC day."""
        meta = self._load_meta()
        last_refresh = str(meta.get("prices_last_refresh") or "")

        if self.prices_gz_path.exists() and self._is_same_utc_day(last_refresh):
            return

        response = requests.get(ALL_PRICES_TODAY_URL, timeout=DEFAULT_TIMEOUT)
        response.raise_for_status()
        with open(self.prices_gz_path, "wb") as handle:
            handle.write(response.content)

        self._rebuild_price_index_from_gzip()
        meta["prices_last_refresh"] = self._utc_now_iso()
        self._save_meta(meta)

    def _latest_numeric_value(self, raw):
        if raw is None:
            return None
        if isinstance(raw, (int, float)):
            return float(raw)
        if isinstance(raw, str):
            try:
                return float(raw)
            except ValueError:
                return None
        if isinstance(raw, list):
            latest = None
            for item in raw:
                value = self._latest_numeric_value(item)
                if value is not None:
                    latest = value
            return latest
        if isinstance(raw, dict):
            # MTGJSON price points are typically date->value
            if not raw:
                return None
            latest_key = sorted(raw.keys())[-1]
            return self._latest_numeric_value(raw.get(latest_key))
        return None

    def _rebuild_price_index_from_gzip(self) -> None:
        with gzip.open(self.prices_gz_path, "rt", encoding="utf-8") as handle:
            payload = json.load(handle)

        root_data = payload.get("data") if isinstance(payload, dict) else {}
        if not isinstance(root_data, dict):
            root_data = {}

        now_iso = self._utc_now_iso()
        rows: list[tuple[str, str, str, str, float, str]] = []

        for uuid, card_prices in root_data.items():
            if not isinstance(card_prices, dict):
                continue

            paper = card_prices.get("paper")
            if not isinstance(paper, dict):
                continue

            for provider, provider_payload in paper.items():
                if not isinstance(provider_payload, dict):
                    continue

                for side in ("retail", "buylist"):
                    side_payload = provider_payload.get(side)
                    if not isinstance(side_payload, dict):
                        continue

                    for finish in ("normal", "foil", "etched"):
                        raw_value = side_payload.get(finish)
                        numeric_value = self._latest_numeric_value(raw_value)
                        if numeric_value is None:
                            continue
                        rows.append((str(uuid), str(provider), side, finish, float(numeric_value), now_iso))

        with self._connect() as conn:
            conn.execute("DELETE FROM price_index")
            if rows:
                conn.executemany(
                    """
                    INSERT INTO price_index (uuid, provider, side, finish, price, updated_at)
                    VALUES (?, ?, ?, ?, ?, ?)
                    """,
                    rows,
                )

    def _normalize_set_code(self, set_code: str) -> str:
        return str(set_code or "").strip().upper()

    def _normalize_collector_number(self, collector_number: str) -> str:
        return str(collector_number or "").strip()

    def _normalize_finish(self, finish: str) -> str:
        value = str(finish or "unknown").strip().lower()
        if value == "nonfoil":
            return "normal"
        if value in SUPPORTED_FINISHES:
            return value
        return "normal"

    def _set_is_fresh(self, set_code: str, max_age_hours: int = 24 * 7) -> bool:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT fetched_at FROM set_cache WHERE set_code = ?",
                (set_code,),
            ).fetchone()
        if not row:
            return False
        fetched_at = row[0]
        try:
            then = datetime.fromisoformat(str(fetched_at))
            if then.tzinfo is None:
                then = then.replace(tzinfo=timezone.utc)
        except ValueError:
            return False
        age_seconds = (datetime.now(timezone.utc) - then).total_seconds()
        return age_seconds < (max_age_hours * 3600)

    def ensure_set_cached(self, set_code: str) -> None:
        normalized_set = self._normalize_set_code(set_code)
        if not normalized_set:
            return

        if self._set_is_fresh(normalized_set):
            return

        url = SET_FILE_URL_TEMPLATE.format(set_code=normalized_set)
        response = requests.get(url, timeout=DEFAULT_TIMEOUT)
        if response.status_code == 404:
            return
        response.raise_for_status()

        payload = response.json()
        data_obj = payload.get("data") if isinstance(payload, dict) else {}
        cards = data_obj.get("cards") if isinstance(data_obj, dict) else []
        if not isinstance(cards, list):
            cards = []

        rows: list[tuple[str, str, str, str | None, str, str, str]] = []
        fetched_at = self._utc_now_iso()

        for card in cards:
            if not isinstance(card, dict):
                continue
            uuid = str(card.get("uuid") or "").strip()
            number = self._normalize_collector_number(card.get("number") or "")
            if not uuid or not number:
                continue

            identifiers = card.get("identifiers") if isinstance(card.get("identifiers"), dict) else {}
            scryfall_id = str(identifiers.get("scryfallId") or "").strip().lower() or None
            name = str(card.get("name") or "")
            finishes = card.get("finishes") if isinstance(card.get("finishes"), list) else []
            normalized_finishes = []
            for finish in finishes:
                finish_key = self._normalize_finish(str(finish))
                if finish_key not in normalized_finishes:
                    normalized_finishes.append(finish_key)
            finishes_json = json.dumps(normalized_finishes, ensure_ascii=False)

            rows.append(
                (
                    normalized_set,
                    number,
                    uuid,
                    scryfall_id,
                    name,
                    finishes_json,
                    fetched_at,
                )
            )

        with self._connect() as conn:
            conn.execute("DELETE FROM card_map WHERE set_code = ?", (normalized_set,))
            if rows:
                conn.executemany(
                    """
                    INSERT INTO card_map (
                        set_code, collector_number, mtgjson_uuid, scryfall_id, card_name, finishes_json, fetched_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?)
                    """,
                    rows,
                )
            conn.execute(
                """
                INSERT INTO set_cache (set_code, fetched_at)
                VALUES (?, ?)
                ON CONFLICT(set_code) DO UPDATE SET fetched_at=excluded.fetched_at
                """,
                (normalized_set, fetched_at),
            )

    def _choose_uuid(
        self,
        set_code: str,
        collector_number: str,
        finish: str,
        scryfall_id: str | None,
    ) -> str | None:
        normalized_set = self._normalize_set_code(set_code)
        normalized_number = self._normalize_collector_number(collector_number)
        normalized_finish = self._normalize_finish(finish)
        normalized_scryfall = str(scryfall_id or "").strip().lower() or None

        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT mtgjson_uuid, scryfall_id, finishes_json
                FROM card_map
                WHERE set_code = ? AND collector_number = ?
                """,
                (normalized_set, normalized_number),
            ).fetchall()

        if not rows:
            return None

        if normalized_scryfall:
            for uuid, row_scryfall, _ in rows:
                if str(row_scryfall or "").lower() == normalized_scryfall:
                    return str(uuid)

        if len(rows) == 1:
            return str(rows[0][0])

        for uuid, _, finishes_json in rows:
            try:
                finishes = json.loads(finishes_json or "[]")
            except json.JSONDecodeError:
                finishes = []
            if normalized_finish in finishes:
                return str(uuid)

        return str(rows[0][0])

    def lookup_price(
        self,
        *,
        set_code: str,
        collector_number: str,
        finish: str,
        scryfall_id: str | None,
        policy: PricePolicy,
    ) -> tuple[float | None, str | None]:
        """Return (price, mtgjson_uuid) if available for the requested card + policy."""
        self.ensure_daily_prices_ready()
        self.ensure_set_cached(set_code)

        uuid = self._choose_uuid(
            set_code=set_code,
            collector_number=collector_number,
            finish=finish,
            scryfall_id=scryfall_id,
        )
        if not uuid:
            return None, None

        finish_key = self._normalize_finish(finish)
        with self._connect() as conn:
            row = conn.execute(
                """
                SELECT price
                FROM price_index
                WHERE uuid = ? AND provider = ? AND side = ? AND finish = ?
                """,
                (uuid, policy.provider, policy.side, finish_key),
            ).fetchone()

            if not row and finish_key != "normal":
                # graceful fallback for cards without foil/etched quotes
                row = conn.execute(
                    """
                    SELECT price
                    FROM price_index
                    WHERE uuid = ? AND provider = ? AND side = ? AND finish = 'normal'
                    """,
                    (uuid, policy.provider, policy.side),
                ).fetchone()

        if not row:
            return None, uuid

        try:
            return float(row[0]), uuid
        except (TypeError, ValueError):
            return None, uuid

    def resolve_uuid(
        self,
        *,
        set_code: str,
        collector_number: str,
        finish: str,
        scryfall_id: str | None,
    ) -> str | None:
        """Resolve MTGJSON UUID from set/collector/finish (and optional scryfall id)."""
        self.ensure_set_cached(set_code)
        return self._choose_uuid(
            set_code=set_code,
            collector_number=collector_number,
            finish=finish,
            scryfall_id=scryfall_id,
        )
