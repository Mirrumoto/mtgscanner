"""
scanner_engine.py — Refactored scanning pipeline with callbacks for GUI integration.

Public function:
    scan_with_callbacks(
        image_folder: str,
        output_path: str,
        provider: str,
        vision_model: str | None,
        on_card_identified: callable,
        on_status: callable,
        on_error: callable,
    ) -> dict

Callbacks:
    - on_card_identified(name, set_code, number, count, match_method, finish, name_confidence, set_confidence, finish_confidence)
  - on_status(message)
  - on_error(message, is_debug_visible)
"""

import json
import shutil
from datetime import datetime
from pathlib import Path

import vision
from pricing import PricingConfig, PricingService

IMAGE_SUFFIXES = {".jpg", ".jpeg", ".png", ".webp"}


def _normalize_confidence(value: str | None) -> str:
    confidence = str(value or "unknown").strip().lower()
    if confidence not in {"high", "medium", "low", "unknown"}:
        return "unknown"
    return confidence


def _apply_finish_policy(candidate: dict, resolved: dict) -> str:
    """Apply conservative finish policy to reduce false foil positives."""
    detected_finish = str(candidate.get("finish") or "unknown").strip().lower()
    if detected_finish not in {"foil", "nonfoil", "unknown"}:
        detected_finish = "unknown"

    if detected_finish != "foil":
        return detected_finish

    finish_confidence = _normalize_confidence(candidate.get("finish_confidence"))
    name_confidence = _normalize_confidence(candidate.get("name_confidence"))
    set_confidence = _normalize_confidence(candidate.get("set_confidence"))

    if finish_confidence != "high" or name_confidence == "low" or set_confidence == "low":
        return "unknown"

    finishes = resolved.get("finishes") or []
    if not isinstance(finishes, list):
        finishes = []
    normalized_finishes = {str(f).strip().lower() for f in finishes}

    has_nonfoil_option = "nonfoil" in normalized_finishes
    if not has_nonfoil_option:
        return "foil"

    frame_effects = resolved.get("frame_effects") or []
    if not isinstance(frame_effects, list):
        frame_effects = []
    normalized_effects = {str(effect).strip().lower() for effect in frame_effects}

    full_art_like = bool(resolved.get("full_art")) or bool(
        normalized_effects.intersection({"extendedart", "showcase", "borderless"})
    )

    if full_art_like:
        return "unknown"

    return "foil"


def _extract_image_url(card_data: dict) -> str:
    """Pick the best available card art URL from a resolved Scryfall payload."""
    image_uris = card_data.get("image_uris") or {}
    if not isinstance(image_uris, dict):
        return ""
    for key in ("small", "normal", "large", "png"):
        value = image_uris.get(key)
        if value:
            return str(value)
    return ""


def _collect_images(folder: Path) -> list[Path]:
    images = sorted(
        p for p in folder.iterdir()
        if p.is_file() and p.suffix.lower() in IMAGE_SUFFIXES
    )
    return images


def _merge(collection: dict, card_data: dict) -> None:
    """
    Merge a resolved Scryfall card into the collection dict.

    Key = "Name [SET #num] (finish)" so cards that are identical in every way
    (name, printing, finish) share one entry whose count increments. Any
    difference in set, collector number, or finish creates a separate entry.
    """
    name = (card_data.get("name") or "").strip()
    set_code = (card_data.get("set") or "").strip().lower()
    collector_number = str(card_data.get("collector_number") or "").strip()

    if not name or not set_code or not collector_number:
        return

    detected_finish = str(card_data.get("finish") or "unknown").strip().lower()
    if detected_finish not in {"foil", "nonfoil", "unknown"}:
        detected_finish = "unknown"

    key = f"{name} [{set_code.upper()} #{collector_number}] ({detected_finish})"

    if key in collection:
        collection[key]["count"] += 1
    else:
        collection[key] = {
            "count": 1,
            **card_data,
            "finish": detected_finish,
        }


def _unresolved_match_context(candidate: dict) -> tuple[list[str], str]:
    """
    Infer which Scryfall lookup methods were attempted for this candidate.
    """
    name = (candidate.get("name") or "").strip()
    set_code = (candidate.get("set_code") or "").strip()
    collector_number = str(candidate.get("collector_number") or "").strip()

    attempted: list[str] = []

    if set_code and collector_number:
        attempted.append("set+number")
        if name:
            attempted.append("name+set")
            attempted.append("name-only")
    elif name and set_code:
        attempted.append("name+set")
        attempted.append("name-only")
    elif name:
        attempted.append("name-only")

    failed_after = attempted[-1] if attempted else "none"
    return attempted, failed_after


def _load_existing_collection(output_path: str, on_error) -> dict:
    """Load existing output JSON so new scans append by default.

    Supports both the current key format "Name [SET #num] (finish)" and the
    legacy format "Name [SET #num]" (which tracked finish via separate count
    fields). Legacy entries are migrated into per-finish entries on load.
    """
    output_file = Path(output_path)
    if not output_file.exists():
        return {}

    try:
        with open(output_file, "r", encoding="utf-8") as f:
            existing = json.load(f)
    except (json.JSONDecodeError, OSError) as exc:
        on_error(f"Could not read existing output JSON, starting fresh: {exc}", debug=True)
        return {}

    if not isinstance(existing, dict):
        on_error("Existing output JSON is not an object, starting fresh.", debug=True)
        return {}

    _FINISH_SUFFIXES = (" (foil)", " (nonfoil)", " (unknown)")

    normalized: dict = {}
    for key, card_data in existing.items():
        if not isinstance(card_data, dict):
            continue

        # Current format — key already contains finish suffix
        if any(key.endswith(s) for s in _FINISH_SUFFIXES):
            try:
                count = int(card_data.get("count", 1) or 1)
            except (TypeError, ValueError):
                count = 1
            normalized[key] = {**card_data, "count": max(count, 1)}
            continue

        # Legacy format — migrate by splitting into per-finish entries
        try:
            count = int(card_data.get("count", 1) or 1)
        except (TypeError, ValueError):
            count = 1

        foil_count = int(card_data.get("foil_count", 0) or 0)
        nonfoil_count = int(card_data.get("nonfoil_count", 0) or 0)
        unknown_count = int(card_data.get("unknown_finish_count", 0) or 0)

        if foil_count == 0 and nonfoil_count == 0 and unknown_count == 0:
            finish = str(card_data.get("finish") or "unknown").strip().lower()
            if finish == "foil":
                foil_count = max(count, 1)
            elif finish == "nonfoil":
                nonfoil_count = max(count, 1)
            else:
                unknown_count = max(count, 1)

        base_data = {k: v for k, v in card_data.items()
                     if k not in ("count", "foil_count", "nonfoil_count",
                                  "unknown_finish_count", "finish")}

        if foil_count > 0:
            new_key = f"{key} (foil)"
            prev = int(normalized.get(new_key, {}).get("count", 0) or 0)
            normalized[new_key] = {**base_data, "finish": "foil",
                                    "count": prev + foil_count}
        if nonfoil_count > 0:
            new_key = f"{key} (nonfoil)"
            prev = int(normalized.get(new_key, {}).get("count", 0) or 0)
            normalized[new_key] = {**base_data, "finish": "nonfoil",
                                    "count": prev + nonfoil_count}
        if unknown_count > 0:
            new_key = f"{key} (unknown)"
            prev = int(normalized.get(new_key, {}).get("count", 0) or 0)
            normalized[new_key] = {**base_data, "finish": "unknown",
                                    "count": prev + unknown_count}

        # Edge case: all sub-counts still zero after normalisation
        if foil_count == 0 and nonfoil_count == 0 and unknown_count == 0:
            new_key = f"{key} (unknown)"
            normalized[new_key] = {**base_data, "finish": "unknown",
                                    "count": max(count, 1)}

    return normalized


def _create_backup_if_exists(file_path: Path, on_status=None, on_error=None) -> Path | None:
    """Create a timestamped rollback backup if the target file already exists."""
    if not file_path.exists() or not file_path.is_file():
        return None

    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    backup_path = file_path.with_name(f"{file_path.stem}.backup.{timestamp}{file_path.suffix}")
    try:
        shutil.copy2(file_path, backup_path)
    except OSError as exc:
        if on_error:
            on_error(f"Could not create rollback backup for {file_path.name}: {exc}", debug=True)
        return None

    if on_status:
        on_status(f"Rollback backup created: {backup_path.name}")

    backup_pattern = f"{file_path.stem}.backup.*{file_path.suffix}"
    backups = sorted(
        file_path.parent.glob(backup_pattern),
        key=lambda path: path.stat().st_mtime,
        reverse=True,
    )

    for stale_backup in backups[1:]:
        try:
            stale_backup.unlink()
        except OSError as exc:
            if on_error:
                on_error(
                    f"Could not remove old backup {stale_backup.name}: {exc}",
                    debug=True,
                )

    return backup_path


def scan_with_callbacks(
    image_folder: str,
    output_path: str,
    provider: str = "gemini",
    vision_model: str | None = None,
    pricing_source: str = "mtgjson",
    pricing_provider: str = "tcgplayer",
    pricing_side: str = "retail",
    pricing_fallback_to_scryfall: bool = True,
    on_card_identified=None,
    on_status=None,
    on_error=None,
    cancel_event=None,
    persist_output: bool = True,
    append_existing: bool = True,
) -> dict:
    """
    Scan images and emit callbacks for GUI integration.

    Args:
        cancel_event: threading.Event that can be set to stop scanning gracefully.

    Returns: { "success": bool, "cards": dict, "unresolved": list, "message": str }
    """
    if on_card_identified is None:
        on_card_identified = lambda **kw: None
    if on_status is None:
        on_status = lambda msg: None
    if on_error is None:
        on_error = lambda msg, debug=False: None

    folder = Path(image_folder)
    if not folder.is_dir():
        msg = f"ERROR: '{image_folder}' is not a valid directory."
        on_error(msg, debug=True)
        return {"success": False, "message": msg, "cards": {}, "unresolved": []}

    images = _collect_images(folder)
    if not images:
        msg = f"No supported images found in '{image_folder}'."
        on_status(msg)
        return {"success": True, "message": msg, "cards": {}, "unresolved": []}

    on_status(f"Found {len(images)} image(s). Starting scan…")

    app_data_dir = Path(output_path).resolve().parent
    pricing_service = PricingService(app_data_dir=app_data_dir)
    pricing_config = PricingConfig(
        source=pricing_source,
        provider=pricing_provider,
        side=pricing_side,
        fallback_to_scryfall=pricing_fallback_to_scryfall,
    )

    collection = _load_existing_collection(output_path, on_error=on_error) if append_existing else {}
    if collection:
        existing_total = sum(v.get("count", 0) for v in collection.values())
        on_status(
            f"Append mode: loaded {len(collection)} existing unique cards ({existing_total} copies)."
        )
    unresolved = []
    detections = []

    for i, img_path in enumerate(images, start=1):
        # Check for cancellation before processing each image
        if cancel_event and cancel_event.is_set():
            on_status("Scan cancelled by user.")
            break

        on_status(f"[{i}/{len(images)}] Processing {img_path.name} …")

        candidates = vision.identify_cards(
            str(img_path),
            provider=provider,
            model=vision_model,
        )

        if not candidates:
            on_status(f"  → No cards identified in {img_path.name}")
            continue

        for candidate in candidates:
            resolved = pricing_service.resolve(candidate, pricing_config)
            if resolved:
                resolved["finish"] = _apply_finish_policy(candidate, resolved)
                resolved["name_confidence"] = candidate.get("name_confidence", candidate.get("confidence", "unknown"))
                resolved["set_confidence"] = candidate.get("set_confidence", candidate.get("confidence", "unknown"))
                resolved["finish_confidence"] = candidate.get("finish_confidence", candidate.get("confidence", "unknown"))
                _merge(collection, resolved)
                name = resolved.get("name", "?")
                set_code = resolved.get("set", "?").upper()
                number = resolved.get("collector_number", "?")
                finish = resolved.get("finish", "unknown")
                coll_key = f"{name} [{set_code} #{number}] ({finish})"
                count = collection.get(coll_key, {}).get("count", 1)
                name_confidence  = resolved.get("name_confidence", "unknown")
                set_confidence   = resolved.get("set_confidence", "unknown")
                finish_confidence = resolved.get("finish_confidence", "unknown")
                on_status(
                    f"      confidence: name={name_confidence}  set={set_confidence}  finish={finish_confidence}  [{finish}]"
                )
                on_card_identified(
                    name=name,
                    set_code=set_code,
                    number=number,
                    count=count,
                    match_method=resolved.get("match_method", "unknown"),
                    finish=finish,
                    name_confidence=resolved.get("name_confidence", "unknown"),
                    set_confidence=resolved.get("set_confidence", "unknown"),
                    finish_confidence=resolved.get("finish_confidence", "unknown"),
                    image_url=_extract_image_url(resolved),
                )
                detections.append(
                    {
                        "name": name,
                        "set": resolved.get("set", "").upper(),
                        "set_name": resolved.get("set_name", ""),
                        "collector_number": str(resolved.get("collector_number", "")),
                        "rarity": resolved.get("rarity", "unknown"),
                        "prices": resolved.get("prices") if isinstance(resolved.get("prices"), dict) else {},
                        "mtgjson_uuid": resolved.get("mtgjson_uuid"),
                        "finish": finish,
                        "image_url": _extract_image_url(resolved),
                        "match_method": resolved.get("match_method", "unknown"),
                        "name_confidence": resolved.get("name_confidence", "unknown"),
                        "set_confidence": resolved.get("set_confidence", "unknown"),
                        "finish_confidence": resolved.get("finish_confidence", "unknown"),
                    }
                )
            else:
                attempted_methods, failed_after = _unresolved_match_context(candidate)
                unresolved.append({
                    "source_image": img_path.name,
                    "attempted_methods": attempted_methods,
                    "failed_after": failed_after,
                    **candidate,
                })
                on_error(
                    f"Unresolved: {candidate.get('name', '?')} "
                    f"[{candidate.get('set_code', '?')}] "
                    f"#{candidate.get('collector_number', '?')}",
                    debug=False,
                )

    output = dict(sorted(collection.items()))

    output_file = Path(output_path)
    unresolved_output_file = output_file.with_name(f"{output_file.stem}.unresolved{output_file.suffix}")

    if persist_output:
        _create_backup_if_exists(output_file, on_status=on_status, on_error=on_error)

        with open(output_path, "w", encoding="utf-8") as f:
            json.dump(output, f, indent=2, ensure_ascii=False)

        if unresolved:
            with open(unresolved_output_file, "w", encoding="utf-8") as f:
                json.dump(unresolved, f, indent=2, ensure_ascii=False)

    total_cards = sum(v["count"] for v in collection.values())
    unique_cards = len(collection)

    if persist_output:
        msg = (
            f"Done. Unique cards: {unique_cards}, Total copies: {total_cards}. "
            f"Output: {output_path}"
        )
        if unresolved:
            msg += f" | Unresolved: {len(unresolved)} (see {unresolved_output_file})"
    else:
        msg = (
            f"Done. Unique cards: {unique_cards}, Total copies: {total_cards}. "
            "Review pending changes and click Save to persist."
        )
        if unresolved:
            msg += f" | Unresolved: {len(unresolved)}"

    on_status(msg)

    return {
        "success": True,
        "message": msg,
        "cards": output,
        "unresolved": unresolved,
        "detections": detections,
    }
