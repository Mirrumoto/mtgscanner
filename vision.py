"""
vision.py — Vision provider abstraction for MTG binder card identification.

Supports:
    - OpenAI (default): gpt-4o
    - Gemini: gemini-2.5-flash

Returns a list of raw candidate dicts:
        {
            "name": str|None,
            "set_code": str|None,
            "collector_number": str|None,
            "confidence": str,
            "name_confidence": str,
            "set_confidence": str,
            "finish_confidence": str,
            "finish": "foil"|"nonfoil"|"unknown"
        }
"""

import base64
import io
import json
import os
import time
from pathlib import Path

from openai import OpenAI
from dotenv import load_dotenv
from google import genai
from google.genai import types
from PIL import Image, ImageEnhance, ImageFilter, ImageOps

load_dotenv()

MAX_RETRIES = 10
INITIAL_RETRY_DELAY_SECONDS = 2.0
MAX_RETRY_DELAY_SECONDS = 6.0
PREPROCESS_MAX_EDGE = 2200

# ── JSON schema for Structured Outputs ────────────────────────────────────────
_CARD_SCHEMA = {
    "type": "json_schema",
    "json_schema": {
        "name": "binder_cards",
        "strict": True,
        "schema": {
            "type": "object",
            "properties": {
                "cards": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "name": {"type": ["string", "null"]},
                            "set_code": {"type": ["string", "null"]},
                            "collector_number": {"type": ["string", "null"]},
                            "confidence": {
                                "type": "string",
                                "enum": ["high", "medium", "low"],
                            },
                            "name_confidence": {
                                "type": "string",
                                "enum": ["high", "medium", "low", "unknown"],
                            },
                            "set_confidence": {
                                "type": "string",
                                "enum": ["high", "medium", "low", "unknown"],
                            },
                            "finish_confidence": {
                                "type": "string",
                                "enum": ["high", "medium", "low", "unknown"],
                            },
                            "finish": {
                                "type": "string",
                                "enum": ["foil", "nonfoil", "unknown"],
                            },
                        },
                        "required": [
                            "name",
                            "set_code",
                            "collector_number",
                            "confidence",
                            "name_confidence",
                            "set_confidence",
                            "finish_confidence",
                            "finish",
                        ],
                        "additionalProperties": False,
                    },
                }
            },
            "required": ["cards"],
            "additionalProperties": False,
        },
    },
}

_SYSTEM_PROMPT = """You are an expert Magic: The Gathering card identifier.

When given a photo of a binder page, carefully examine every visible card pocket.
For each card you can identify, return:
  - name: the full English card name exactly as printed (e.g. "Lightning Bolt")
  - set_code: the 3–5 letter set code printed in small text at the bottom-left of the card (e.g. "m10", "mkc", "dom")
  - collector_number: the collector number printed at the bottom of the card (e.g. "146", "96a")
  - confidence: "high" if you are certain, "medium" if somewhat sure, "low" if guessing
    - name_confidence: confidence for card name only (high/medium/low/unknown)
    - set_confidence: confidence for set_code only (high/medium/low/unknown)
    - finish: "foil" if card appears foil, "nonfoil" if it appears regular, "unknown" if uncertain
    - finish_confidence: confidence for finish only (high/medium/low/unknown)

Rules:
- If a pocket is empty, skip it entirely.
- If a field is unreadable due to glare, angle, or obstruction, return null for that field — do NOT guess.
- Foil classification must be conservative:
  - Mark "foil" ONLY when there is clear foil-specific glare/rainbow specular reflection on the card surface.
  - Do NOT use art style, color richness, or frame treatment as foil evidence.
  - Full-art, borderless, showcase, and textured-looking artwork are often non-foil; do not assume foil.
    - If you are deciding between nonfoil and foil on a full-art/borderless card, prefer nonfoil unless foil evidence is unmistakable.
  - If uncertain, prefer "unknown" (not "foil").
- Return one entry per card. Do not duplicate cards.
- Set codes are lowercase (e.g. "xln" not "XLN").
- Collector numbers are strings (e.g. "146", not 146).
"""


def _normalize_finish_candidates(cards: list[dict]) -> list[dict]:
    """Apply conservative finish normalization to reduce foil false positives."""
    normalized_cards: list[dict] = []

    for card in cards:
        if not isinstance(card, dict):
            continue

        normalized = dict(card)
        finish = str(normalized.get("finish") or "unknown").strip().lower()
        confidence = str(normalized.get("confidence") or "low").strip().lower()
        name_confidence = str(normalized.get("name_confidence") or confidence or "unknown").strip().lower()
        set_confidence = str(normalized.get("set_confidence") or confidence or "unknown").strip().lower()
        finish_confidence = str(normalized.get("finish_confidence") or confidence or "unknown").strip().lower()

        allowed_confidence = {"high", "medium", "low", "unknown"}
        if name_confidence not in allowed_confidence:
            name_confidence = "unknown"
        if set_confidence not in allowed_confidence:
            set_confidence = "unknown"
        if finish_confidence not in allowed_confidence:
            finish_confidence = "unknown"

        if finish not in {"foil", "nonfoil", "unknown"}:
            finish = "unknown"

        has_set = bool((normalized.get("set_code") or "").strip())
        has_number = bool(str(normalized.get("collector_number") or "").strip())

        if finish == "foil" and (finish_confidence != "high" or confidence != "high" or not (has_set and has_number)):
            finish = "unknown"
            finish_confidence = "unknown"

        normalized["finish"] = finish
        normalized["name_confidence"] = name_confidence
        normalized["set_confidence"] = set_confidence
        normalized["finish_confidence"] = finish_confidence
        normalized_cards.append(normalized)

    return normalized_cards


def _preprocess_image_bytes(image_path: str) -> tuple[bytes, str]:
    """Apply lightweight preprocessing and return image bytes + mime type."""
    try:
        with Image.open(image_path) as img:
            processed = ImageOps.exif_transpose(img)

            if processed.mode not in ("RGB", "L"):
                processed = processed.convert("RGB")
            elif processed.mode == "L":
                processed = processed.convert("RGB")

            width, height = processed.size
            longest_edge = max(width, height)
            if longest_edge > PREPROCESS_MAX_EDGE:
                scale = PREPROCESS_MAX_EDGE / longest_edge
                new_size = (
                    max(1, int(width * scale)),
                    max(1, int(height * scale)),
                )
                processed = processed.resize(new_size, Image.Resampling.LANCZOS)

            processed = ImageOps.autocontrast(processed, cutoff=1)
            processed = ImageEnhance.Contrast(processed).enhance(1.05)
            processed = processed.filter(ImageFilter.UnsharpMask(radius=1.1, percent=110, threshold=3))

            out = io.BytesIO()
            processed.save(out, format="JPEG", quality=90, optimize=True)
            return out.getvalue(), "image/jpeg"

    except Exception as exc:
        print(f"  [vision] preprocess fallback for {Path(image_path).name}: {exc}")
        suffix = Path(image_path).suffix.lower()
        mime_map = {".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".png": "image/png", ".webp": "image/webp"}
        mime = mime_map.get(suffix, "image/jpeg")
        with open(image_path, "rb") as f:
            return f.read(), mime


def _encode_image(image_path: str) -> tuple[str, str]:
    """Return (base64_string, mime_type) for a preprocessed local image file."""
    image_bytes, mime = _preprocess_image_bytes(image_path)
    return base64.standard_b64encode(image_bytes).decode("utf-8"), mime


def _is_transient_error(error_text: str) -> bool:
    """Best-effort detection of retryable provider/API failures."""
    text = error_text.lower()
    transient_markers = [
        "429",
        "500",
        "502",
        "503",
        "504",
        "rate limit",
        "timeout",
        "temporar",
        "unavailable",
        "high demand",
        "overloaded",
    ]
    return any(marker in text for marker in transient_markers)


def _identify_cards_openai(image_path: str, model: str) -> list[dict]:
    """
    Send image_path to OpenAI Vision and return a list of card candidates.

    Each candidate: {
        name, set_code, collector_number, confidence,
        name_confidence, set_confidence, finish_confidence, finish
    }
    Returns an empty list on failure.
    """
    client = OpenAI(api_key=os.environ.get("OPENAI_API_KEY"))

    b64, mime = _encode_image(image_path)
    data_uri = f"data:{mime};base64,{b64}"

    delay = INITIAL_RETRY_DELAY_SECONDS

    for attempt in range(1, MAX_RETRIES + 1):
        try:
            response = client.chat.completions.create(
                model=model,
                response_format=_CARD_SCHEMA,
                messages=[
                    {"role": "system", "content": _SYSTEM_PROMPT},
                    {
                        "role": "user",
                        "content": [
                            {
                                "type": "image_url",
                                "image_url": {"url": data_uri, "detail": "high"},
                            },
                            {
                                "type": "text",
                                "text": "This is a binder page with up to 16 card pockets (4×4 grid). Identify every visible card.",
                            },
                        ],
                    },
                ],
                max_tokens=2000,
            )

            raw = response.choices[0].message.content
            parsed = json.loads(raw)
            cards = _normalize_finish_candidates(parsed.get("cards", []))
            print(f"  [vision] Found {len(cards)} card(s) in {Path(image_path).name}")
            return cards

        except Exception as exc:
            err = str(exc)
            is_transient = _is_transient_error(err)
            is_last_attempt = attempt >= MAX_RETRIES

            if is_transient and not is_last_attempt:
                print(
                    f"  [vision:openai] transient error on {Path(image_path).name} "
                    f"(attempt {attempt}/{MAX_RETRIES}): {exc}"
                )
                print(f"  [vision:openai] retrying in {delay:.1f}s …")
                time.sleep(delay)
                delay = min(delay * 2, MAX_RETRY_DELAY_SECONDS)
                continue

            print(f"  [vision:openai] ERROR processing {Path(image_path).name}: {exc}")
            return []

    return []


def _identify_cards_gemini(image_path: str, model: str) -> list[dict]:
    """
    Send image_path to Gemini Vision and return a list of card candidates.
    """
    api_key = os.environ.get("GEMINI_API_KEY")
    if not api_key:
        print("  [vision:gemini] ERROR: GEMINI_API_KEY is not set.")
        return []

    client = genai.Client(api_key=api_key)

    image_bytes, mime = _preprocess_image_bytes(image_path)

    schema = {
        "type": "OBJECT",
        "properties": {
            "cards": {
                "type": "ARRAY",
                "items": {
                    "type": "OBJECT",
                    "properties": {
                        "name": {"type": "STRING", "nullable": True},
                        "set_code": {"type": "STRING", "nullable": True},
                        "collector_number": {"type": "STRING", "nullable": True},
                        "confidence": {"type": "STRING", "enum": ["high", "medium", "low"]},
                        "name_confidence": {"type": "STRING", "enum": ["high", "medium", "low", "unknown"]},
                        "set_confidence": {"type": "STRING", "enum": ["high", "medium", "low", "unknown"]},
                        "finish_confidence": {"type": "STRING", "enum": ["high", "medium", "low", "unknown"]},
                        "finish": {"type": "STRING", "enum": ["foil", "nonfoil", "unknown"]},
                    },
                    "required": [
                        "name",
                        "set_code",
                        "collector_number",
                        "confidence",
                        "name_confidence",
                        "set_confidence",
                        "finish_confidence",
                        "finish",
                    ],
                },
            }
        },
        "required": ["cards"],
    }

    delay = INITIAL_RETRY_DELAY_SECONDS

    for attempt in range(1, MAX_RETRIES + 1):
        try:
            response = client.models.generate_content(
                model=model,
                contents=[
                    _SYSTEM_PROMPT,
                    "This is a binder page with up to 16 card pockets (4×4 grid). Identify every visible card.",
                    types.Part.from_bytes(data=image_bytes, mime_type=mime),
                ],
                config=types.GenerateContentConfig(
                    response_mime_type="application/json",
                    response_schema=schema,
                ),
            )

            raw = response.text or ""
            parsed = json.loads(raw)
            cards = _normalize_finish_candidates(parsed.get("cards", []))
            print(f"  [vision] Found {len(cards)} card(s) in {Path(image_path).name}")
            return cards

        except Exception as exc:
            err = str(exc)
            is_transient = _is_transient_error(err)
            is_last_attempt = attempt >= MAX_RETRIES

            if is_transient and not is_last_attempt:
                print(
                    f"  [vision:gemini] transient error on {Path(image_path).name} "
                    f"(attempt {attempt}/{MAX_RETRIES}): {exc}"
                )
                print(f"  [vision:gemini] retrying in {delay:.1f}s …")
                time.sleep(delay)
                delay = min(delay * 2, MAX_RETRY_DELAY_SECONDS)
                continue

            print(f"  [vision:gemini] ERROR processing {Path(image_path).name}: {exc}")
            return []

    return []


def identify_cards(
    image_path: str,
    provider: str = "openai",
    model: str | None = None,
) -> list[dict]:
    """
    Identify cards with the requested provider.

    provider: "openai" | "gemini"
    model: optional model override
    """
    provider = provider.lower().strip()

    if provider == "openai":
        chosen_model = model or "gpt-4o"
        return _identify_cards_openai(image_path, chosen_model)

    if provider == "gemini":
        chosen_model = model or "gemini-2.5-flash"
        return _identify_cards_gemini(image_path, chosen_model)

    print(f"  [vision] ERROR: Unsupported provider '{provider}'. Use 'openai' or 'gemini'.")
    return []
