"""
vision.py — Vision provider abstraction for MTG binder card identification.

Supports:
    - Gemini: gemini-2.5-flash
    - Unsloth: local OpenAI-compatible endpoint (Gemma/llama.cpp/Ollama)

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
import re
import time
from pathlib import Path

import requests
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
OPENAI_COMPAT_FALLBACK_PROMPT = (
    "Return ONLY valid JSON that matches the expected schema. "
    "Do not include markdown fences or extra text."
)
_IMAGE_INSTRUCTION = "This is a binder page with up to 16 card pockets (4×4 grid). Identify every visible card."
_REPAIR_SYSTEM_PROMPT = "You are a strict JSON repair utility."
_REPAIR_USER_TEMPLATE = (
    "Convert the following model output into strict JSON with this exact shape:\n"
    "{\"cards\": [{\"name\": string|null, \"set_code\": string|null, \"collector_number\": string|null, "
    "\"confidence\": \"high\"|\"medium\"|\"low\", \"name_confidence\": \"high\"|\"medium\"|\"low\"|\"unknown\", "
    "\"set_confidence\": \"high\"|\"medium\"|\"low\"|\"unknown\", \"finish_confidence\": \"high\"|\"medium\"|\"low\"|\"unknown\", "
    "\"finish\": \"foil\"|\"nonfoil\"|\"unknown\"}]}\n"
    "Return only JSON. No markdown, no explanation.\n\n"
    "Raw output:\n{raw_output}"
)

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


def _load_prompt_config() -> dict:
    config_path = str(os.environ.get("VISION_PROMPTS_PATH") or "").strip()
    if config_path:
        path = Path(config_path)
    else:
        path = Path(__file__).resolve().parent / "vision_prompts.json"

    if not path.exists() or not path.is_file():
        return {}

    try:
        with open(path, "r", encoding="utf-8") as handle:
            parsed = json.load(handle)
        return parsed if isinstance(parsed, dict) else {}
    except Exception as exc:
        print(f"  [vision] prompt config fallback: could not load {path.name}: {exc}")
        return {}


_PROMPT_CONFIG = _load_prompt_config()
_SYSTEM_PROMPT = str(_PROMPT_CONFIG.get("system_prompt") or _SYSTEM_PROMPT)
_IMAGE_INSTRUCTION = str(_PROMPT_CONFIG.get("image_instruction") or _IMAGE_INSTRUCTION)
OPENAI_COMPAT_FALLBACK_PROMPT = str(
    _PROMPT_CONFIG.get("fallback_prompt") or OPENAI_COMPAT_FALLBACK_PROMPT
)
_REPAIR_SYSTEM_PROMPT = str(_PROMPT_CONFIG.get("repair_system_prompt") or _REPAIR_SYSTEM_PROMPT)
_REPAIR_USER_TEMPLATE = str(_PROMPT_CONFIG.get("repair_user_template") or _REPAIR_USER_TEMPLATE)


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
        normalized["confidence"] = confidence if confidence in {"high", "medium", "low"} else "low"
        normalized["name_confidence"] = name_confidence
        normalized["set_confidence"] = set_confidence
        normalized["finish_confidence"] = finish_confidence
        normalized_cards.append(normalized)

    return _downgrade_suspicious_batch_patterns(normalized_cards)


def _collector_sequence_value(raw_number: object) -> tuple[int, str] | None:
    text = str(raw_number or "").strip().lower()
    if not text:
        return None

    match = re.fullmatch(r"(\d+)([a-z]?)", text)
    if not match:
        return None

    numeric = int(match.group(1))
    suffix = match.group(2) or ""
    return numeric, suffix


def _downgrade_suspicious_batch_patterns(cards: list[dict]) -> list[dict]:
    """Downgrade obvious batch hallucinations from weaker local models.

    A common failure mode is inventing a run of cards from one set with
    consecutive collector numbers and unjustifiably high confidence.
    When detected, set metadata is cleared so downstream resolution cannot
    auto-lock onto a fabricated exact printing.
    """
    if len(cards) < 4:
        return cards

    grouped: dict[str, list[tuple[int, dict]]] = {}
    for card in cards:
        set_code = str(card.get("set_code") or "").strip().lower()
        sequence_value = _collector_sequence_value(card.get("collector_number"))
        if not set_code or sequence_value is None:
            continue
        grouped.setdefault(set_code, []).append((sequence_value[0], card))

    suspicious_groups: list[str] = []
    for set_code, entries in grouped.items():
        if len(entries) < 4:
            continue

        numeric_values = sorted({value for value, _card in entries})
        if len(numeric_values) < 4:
            continue

        is_consecutive_run = all(
            numeric_values[idx] + 1 == numeric_values[idx + 1]
            for idx in range(len(numeric_values) - 1)
        )
        if is_consecutive_run:
            suspicious_groups.append(set_code)

    if not suspicious_groups:
        return cards

    for card in cards:
        set_code = str(card.get("set_code") or "").strip().lower()
        if set_code not in suspicious_groups:
            continue

        card["set_confidence"] = "low"
        if str(card.get("confidence") or "low").strip().lower() == "high":
            card["confidence"] = "low"

        # Exact set/number matches are dangerous here; force downstream logic
        # to avoid fabricated precise print identification.
        card["set_code"] = None
        card["collector_number"] = None

        if str(card.get("name_confidence") or "unknown").strip().lower() == "high":
            card["name_confidence"] = "medium"

    print(
        "  [vision] suspicious consecutive set-number pattern detected; "
        f"downgraded metadata for set(s): {', '.join(sorted(suspicious_groups))}"
    )
    return cards


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


def _extract_json_payload(raw_text: str) -> str | None:
    text = str(raw_text or "").strip()
    if not text:
        return None

    if "```" in text:
        text = text.replace("```json", "```").replace("```JSON", "```")
        parts = [part.strip() for part in text.split("```") if part.strip()]
        for part in parts:
            if part.startswith("{") and part.endswith("}"):
                return part

    if "<|channel>thought" in text and "<channel|>" in text:
        text = text.split("<channel|>")[-1].strip()

    start = text.find("{")
    end = text.rfind("}")
    if start == -1 or end == -1 or end <= start:
        return None

    return text[start:end + 1]


def _parse_cards_payload(raw_text: str) -> list[dict] | None:
    text = str(raw_text or "").strip()
    if not text:
        return None

    candidates: list[str] = [text]
    extracted = _extract_json_payload(text)
    if extracted and extracted != text:
        candidates.append(extracted)

    for candidate in candidates:
        try:
            parsed = json.loads(candidate)
            cards = parsed.get("cards", [])
            if isinstance(cards, list):
                return _normalize_finish_candidates(cards)
        except Exception:
            continue

    return None


def _list_available_models(base_url: str) -> list[str]:
    models_url = f"{base_url.rstrip('/')}/models"
    try:
        response = requests.get(models_url, timeout=4)
        response.raise_for_status()
        payload = response.json()
        data = payload.get("data", []) if isinstance(payload, dict) else []
        model_ids: list[str] = []
        for item in data:
            if not isinstance(item, dict):
                continue
            model_id = str(item.get("id") or "").strip()
            if model_id:
                model_ids.append(model_id)
        return model_ids
    except Exception:
        return []


def _resolve_unsloth_model_alias(model: str, base_url: str) -> str:
    requested = str(model or "").strip()
    if not requested:
        return requested

    available = _list_available_models(base_url)
    if not available:
        return requested

    if requested in available:
        return requested

    alias_map = {
        "gemma-4-e2b-it": ["gemma4:e2b", "gemma4:latest", "gemma4"],
        "gemma-4-e4b-it": ["gemma4:e4b", "gemma4:latest", "gemma4"],
        "gemma-4-26b-a4b-it": ["gemma4:26b", "gemma4:latest", "gemma4"],
        "gemma-4-31b-it": ["gemma4:31b", "gemma4:latest", "gemma4"],
    }

    preferred_aliases = alias_map.get(requested, [])
    for alias in preferred_aliases:
        if alias in available:
            print(f"  [vision:unsloth] using available model '{alias}' for requested '{requested}'.")
            return alias

    gemma4_like = [model_id for model_id in available if model_id.lower().startswith("gemma4")]
    if gemma4_like:
        chosen = gemma4_like[0]
        print(f"  [vision:unsloth] using available model '{chosen}' for requested '{requested}'.")
        return chosen

    return requested


def _repair_unsloth_cards_payload(client: OpenAI, model: str, raw_text: str, max_tokens: int) -> list[dict] | None:
    repair_prompt = _REPAIR_USER_TEMPLATE.replace("{raw_output}", raw_text)

    try:
        repair = client.chat.completions.create(
            model=model,
            messages=[
                {"role": "system", "content": _REPAIR_SYSTEM_PROMPT},
                {"role": "user", "content": repair_prompt},
            ],
            max_tokens=max_tokens,
            temperature=0.0,
            top_p=0.9,
        )
        repaired_raw = repair.choices[0].message.content or ""
        return _parse_cards_payload(repaired_raw)
    except Exception:
        return None


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
                    _IMAGE_INSTRUCTION,
                    types.Part.from_bytes(data=image_bytes, mime_type=mime),
                ],
                config=types.GenerateContentConfig(
                    response_mime_type="application/json",
                    response_schema=schema,
                ),
            )

            raw = response.text or ""
            cards = _parse_cards_payload(raw)
            if cards is None:
                raise ValueError("Gemini returned non-JSON or invalid schema payload.")
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


def _identify_cards_unsloth(image_path: str, model: str) -> list[dict]:
    """
    Send image_path to an OpenAI-compatible endpoint serving Gemma/Unsloth models.

    Requires:
      - UNSLOTH_BASE_URL
      - UNSLOTH_API_KEY (optional if endpoint runs without auth)
    """
    base_url = os.environ.get("UNSLOTH_BASE_URL", "").strip()
    if not base_url:
        print("  [vision:unsloth] ERROR: UNSLOTH_BASE_URL is not set.")
        return []

    api_key = os.environ.get("UNSLOTH_API_KEY", "").strip() or "unsloth-local"
    client = OpenAI(api_key=api_key, base_url=base_url)
    resolved_model = _resolve_unsloth_model_alias(model, base_url)
    try:
        max_tokens = int(str(os.environ.get("UNSLOTH_MAX_TOKENS") or "2800").strip())
    except ValueError:
        max_tokens = 2800
    max_tokens = max(1000, max_tokens)

    b64, mime = _encode_image(image_path)
    data_uri = f"data:{mime};base64,{b64}"

    delay = INITIAL_RETRY_DELAY_SECONDS

    for attempt in range(1, MAX_RETRIES + 1):
        try:
            response = client.chat.completions.create(
                model=resolved_model,
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
                                "text": _IMAGE_INSTRUCTION,
                            },
                        ],
                    },
                ],
                max_tokens=max_tokens,
                temperature=0.0,
                top_p=0.9,
            )

            raw = response.choices[0].message.content
            cards = _parse_cards_payload(raw)
            if cards is None:
                repaired = _repair_unsloth_cards_payload(client, resolved_model, raw or "", max_tokens=max_tokens)
                if repaired is not None:
                    print(f"  [vision:unsloth] repaired non-JSON output for {Path(image_path).name}.")
                    return repaired
                raise ValueError("Unsloth response did not contain valid cards JSON.")
            print(f"  [vision] Found {len(cards)} card(s) in {Path(image_path).name}")
            return cards

        except Exception as schema_exc:
            schema_error_text = str(schema_exc)
            unsupported_schema = any(
                marker in schema_error_text.lower()
                for marker in ["response_format", "json_schema", "not supported", "unsupported"]
            )

            if not unsupported_schema:
                err = schema_error_text
                is_transient = _is_transient_error(err)
                is_last_attempt = attempt >= MAX_RETRIES

                if is_transient and not is_last_attempt:
                    print(
                        f"  [vision:unsloth] transient error on {Path(image_path).name} "
                        f"(attempt {attempt}/{MAX_RETRIES}): {schema_exc}"
                    )
                    print(f"  [vision:unsloth] retrying in {delay:.1f}s …")
                    time.sleep(delay)
                    delay = min(delay * 2, MAX_RETRY_DELAY_SECONDS)
                    continue

                print(f"  [vision:unsloth] ERROR processing {Path(image_path).name}: {schema_exc}")
                return []

            try:
                response = client.chat.completions.create(
                    model=resolved_model,
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
                                    "text": _IMAGE_INSTRUCTION + " " + OPENAI_COMPAT_FALLBACK_PROMPT,
                                },
                            ],
                        },
                    ],
                    max_tokens=max_tokens,
                    temperature=0.0,
                    top_p=0.9,
                )

                raw = response.choices[0].message.content
                cards = _parse_cards_payload(raw)
                if cards is None:
                    raise ValueError("Unsloth fallback response did not contain valid cards JSON.")
                print(f"  [vision] Found {len(cards)} card(s) in {Path(image_path).name}")
                return cards

            except Exception as fallback_exc:
                err = str(fallback_exc)
                is_transient = _is_transient_error(err)
                is_last_attempt = attempt >= MAX_RETRIES

                if is_transient and not is_last_attempt:
                    print(
                        f"  [vision:unsloth] transient error on {Path(image_path).name} "
                        f"(attempt {attempt}/{MAX_RETRIES}): {fallback_exc}"
                    )
                    print(f"  [vision:unsloth] retrying in {delay:.1f}s …")
                    time.sleep(delay)
                    delay = min(delay * 2, MAX_RETRY_DELAY_SECONDS)
                    continue

                print(f"  [vision:unsloth] ERROR processing {Path(image_path).name}: {fallback_exc}")
                return []

    return []


def identify_cards(
    image_path: str,
    provider: str = "gemini",
    model: str | None = None,
) -> list[dict]:
    """
    Identify cards with the requested provider.

    provider: "gemini" | "unsloth"
    model: optional model override
    """
    provider = provider.lower().strip()

    if provider == "gemini":
        chosen_model = model or "gemini-2.5-flash"
        return _identify_cards_gemini(image_path, chosen_model)

    if provider == "unsloth":
        chosen_model = model or os.environ.get("UNSLOTH_VISION_MODEL", "gemma4:latest")
        return _identify_cards_unsloth(image_path, chosen_model)

    print(f"  [vision] ERROR: Unsupported provider '{provider}'. Use 'gemini' or 'unsloth'.")
    return []
