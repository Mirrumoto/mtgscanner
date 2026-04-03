"""
scryfall.py — Scryfall API lookups with caching, rate limiting, and backoff.

Public function:
    resolve(candidate: dict) -> dict | None

    candidate: { "name": str|None, "set_code": str|None, "collector_number": str|None, ... }

    Returns a Scryfall card object (subset of fields) or None if unresolvable.
"""

import time
import requests

# ── Constants ─────────────────────────────────────────────────────────────────
BASE_URL = "https://api.scryfall.com"
HEADERS  = {"User-Agent": "MTGBinderScanner/1.0", "Accept": "application/json"}
MIN_REQUEST_INTERVAL = 0.25  # ~4 req/s max, conservative to avoid rate limiting
MAX_RETRIES = 5  # for 429 / transient errors

# ── In-memory cache (keyed by resolved Scryfall card id) ──────────────────────
_cache: dict[str, dict] = {}
_last_request_ts = 0.0
_print_options_cache: dict[str, list[dict]] = {}

# Fields to extract from a Scryfall card object
_WANTED_FIELDS = [
    "id", "oracle_id", "name", "lang",
    "mana_cost", "cmc", "type_line", "oracle_text",
    "colors", "color_identity", "keywords",
    "set", "set_name", "collector_number", "rarity",
    "artist", "released_at", "reprint", "promo", "finishes",
    "full_art", "frame_effects", "border_color",
    "image_uris", "prices", "edhrec_rank", "legalities",
]


def _get(url: str, params: dict | None = None) -> dict | None:
    """
    Make a GET request to Scryfall with automatic rate-limit backoff.
    Returns parsed JSON or None on 404 / unrecoverable error.
    """
    global _last_request_ts
    backoff = 0.0

    for attempt in range(MAX_RETRIES):
        now = time.monotonic()
        elapsed = now - _last_request_ts
        wait_for_interval = max(0.0, MIN_REQUEST_INTERVAL - elapsed)
        sleep_for = wait_for_interval + backoff
        if sleep_for > 0:
            time.sleep(sleep_for)

        _last_request_ts = time.monotonic()

        try:
            r = requests.get(url, params=params, headers=HEADERS, timeout=10)
        except requests.RequestException as exc:
            print(f"  [scryfall] Network error: {exc}")
            return None

        if r.status_code == 200:
            return r.json()
        if r.status_code == 404:
            return None  # not found — caller handles
        if r.status_code == 429:
            backoff = min(1.0 if backoff == 0.0 else backoff * 2, 10.0)
            print(f"  [scryfall] 429 rate limit — retrying in {backoff:.1f}s …")
            continue
        # Any other non-200
        print(f"  [scryfall] Unexpected status {r.status_code} for {url}")
        return None

    print(f"  [scryfall] Max retries exceeded for {url}")
    return None


def _extract(card: dict) -> dict:
    """Slim down a Scryfall card object to the fields we care about."""
    result = {k: card.get(k) for k in _WANTED_FIELDS}

    # For MDFCs the top-level image_uris may be absent; pull from front face
    if not result.get("image_uris") and card.get("card_faces"):
        result["image_uris"] = card["card_faces"][0].get("image_uris")

    return result


def _lookup_by_set_number(set_code: str, number: str) -> dict | None:
    """Level 1: most precise — exact set + collector number."""
    url = f"{BASE_URL}/cards/{set_code.lower()}/{number}"
    return _get(url)


def _normalize_card_name(name: str) -> str:
    """
    Handle MDFC (Modal Double-Faced Card) names.
    Scryfall API expects only the front face name (e.g., "Archangel" not "Archangel // Priest").
    """
    if "//" in name:
        return name.split("//")[0].strip()
    return name


def _lookup_by_name_and_set(name: str, set_code: str) -> dict | None:
    """Level 2: fuzzy name + set hint."""
    normalized_name = _normalize_card_name(name)
    return _get(f"{BASE_URL}/cards/named", params={"fuzzy": normalized_name, "set": set_code.lower()})


def _lookup_by_name(name: str) -> dict | None:
    """Level 3: fuzzy name only — no set constraint."""
    normalized_name = _normalize_card_name(name)
    return _get(f"{BASE_URL}/cards/named", params={"fuzzy": normalized_name})


def resolve(candidate: dict) -> dict | None:
    """
    Try to resolve a GPT-4o candidate to a Scryfall card object.

    Waterfall:
      1. set_code + collector_number  →  /cards/:set/:number
      2. name + set_code              →  /cards/named?fuzzy=&set=
      3. name only                    →  /cards/named?fuzzy=
      4. unresolvable                 →  None

    Caches results to avoid duplicate API calls.
    """
    name   = (candidate.get("name") or "").strip()
    setn   = (candidate.get("set_code") or "").strip().lower()
    number = (candidate.get("collector_number") or "").strip()

    # Build a dedupe key for the cache
    cache_key = f"{name.lower()}|{setn}|{number}" if name else None

    if cache_key and cache_key in _cache:
        return _cache[cache_key]

    card = None
    match_method = None

    # Level 1
    if setn and number:
        card = _lookup_by_set_number(setn, number)
        if card:
            match_method = "set+number"
            print(f"    [scryfall] ✓ (set+number)  {card['name']} [{card['set'].upper()} #{card['collector_number']}]")

    # Level 2
    if card is None and name and setn:
        card = _lookup_by_name_and_set(name, setn)
        if card:
            match_method = "name+set"
            print(f"    [scryfall] ✓ (name+set)    {card['name']} [{card['set'].upper()} #{card['collector_number']}]")

    # Level 3
    if card is None and name:
        card = _lookup_by_name(name)
        if card:
            match_method = "name-only"
            print(f"    [scryfall] ✓ (name only)   {card['name']} [{card['set'].upper()} #{card['collector_number']}]")

    if card is None:
        print(f"    [scryfall] ✗ Unresolved: name={name!r} set={setn!r} num={number!r}")
        if cache_key:
            _cache[cache_key] = None  # cache the miss to avoid repeat calls
        return None

    slim = _extract(card)
    slim["match_method"] = match_method or "unknown"

    if cache_key:
        _cache[cache_key] = slim

    return slim


def get_print_options(name: str) -> list[dict]:
    """Return all known set/collector/treatment options for an exact card name."""
    raw_name = (name or "").strip()
    normalized_name = _normalize_card_name(raw_name)
    if not normalized_name:
        return []

    cache_key = normalized_name.lower()
    cached = _print_options_cache.get(cache_key)
    if cached is not None:
        return cached

    query = f'!"{normalized_name}"'
    response = _get(
        f"{BASE_URL}/cards/search",
        params={"q": query, "unique": "prints", "order": "released", "dir": "desc"},
    )
    if not response:
        _print_options_cache[cache_key] = []
        return []

    all_cards: list[dict] = []
    payload = response
    while isinstance(payload, dict):
        data = payload.get("data")
        if isinstance(data, list):
            all_cards.extend(data)

        if not payload.get("has_more"):
            break

        next_page = payload.get("next_page")
        if not isinstance(next_page, str) or not next_page:
            break
        payload = _get(next_page)
        if not payload:
            break

    seen: set[tuple[str, str, str]] = set()
    options: list[dict] = []

    for card in all_cards:
        set_code = str(card.get("set") or "").upper()
        set_name = str(card.get("set_name") or "")
        collector_number = str(card.get("collector_number") or "")
        image_uris = card.get("image_uris") or {}
        if not isinstance(image_uris, dict) and card.get("card_faces"):
            faces = card.get("card_faces") or []
            if faces and isinstance(faces[0], dict):
                image_uris = faces[0].get("image_uris") or {}
        if not isinstance(image_uris, dict):
            image_uris = {}

        image_url = (
            image_uris.get("small")
            or image_uris.get("normal")
            or image_uris.get("large")
            or image_uris.get("png")
            or ""
        )

        finishes = card.get("finishes") or ["unknown"]
        if not isinstance(finishes, list) or not finishes:
            finishes = ["unknown"]

        for finish in finishes:
            normalized_finish = str(finish or "unknown").strip().lower()
            if normalized_finish not in {"foil", "nonfoil", "etched", "unknown"}:
                normalized_finish = "unknown"

            key = (set_code, collector_number, normalized_finish)
            if key in seen:
                continue
            seen.add(key)
            options.append(
                {
                    "name": str(card.get("name") or normalized_name),
                    "set": set_code,
                    "set_name": set_name,
                    "collector_number": collector_number,
                    "rarity": card.get("rarity"),
                    "prices": card.get("prices") if isinstance(card.get("prices"), dict) else {},
                    "finish": normalized_finish,
                    "image_url": str(image_url or ""),
                }
            )

    _print_options_cache[cache_key] = options
    return options
