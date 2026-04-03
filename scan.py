"""
scan.py — Orchestrate MTG binder photo scanning.

Usage:
    python scan.py <image_folder> [--output <path/to/cards.json>]

Examples:
    python scan.py "C:\\Users\\wesle\\repos\\MTG Price lookup"
    python scan.py "C:\\Users\\wesle\\repos\\MTG Price lookup" --output my_collection.json
"""

import argparse
import json
import math
import shutil
import sys
from datetime import datetime
from pathlib import Path

from PIL import Image

import vision
import scryfall

# ── Supported image extensions ────────────────────────────────────────────────
IMAGE_SUFFIXES = {".jpg", ".jpeg", ".png", ".webp"}
BASE_IMAGE_TOKENS = 85
HIGH_DETAIL_TILE_TOKENS = 170
DEFAULT_INPUT_PER_1M = 2.50
DEFAULT_OUTPUT_PER_1M = 10.00
DEFAULT_OUTPUT_TOKENS_LOW = 200
DEFAULT_OUTPUT_TOKENS_TYPICAL = 500
DEFAULT_OUTPUT_TOKENS_HIGH = 900
GEMINI_TIER_MODEL_MAP = {
    "2.5": "gemini-2.5-flash",
    "3": "gemini-3-flash-preview",
}


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


def _collect_images(folder: Path) -> list[Path]:
    images = sorted(
        p for p in folder.iterdir()
        if p.is_file() and p.suffix.lower() in IMAGE_SUFFIXES
    )
    return images


def _estimate_image_input_tokens(image_path: Path) -> int:
    """
    Estimate GPT-4o image input tokens for detail='high'.

    Approximation follows OpenAI vision tile accounting:
    1) Fit image in 2048x2048 box (preserve aspect ratio)
    2) Scale so shortest side is 768
    3) Count 512x512 tiles and apply token formula: 85 + 170 * tiles
    """
    with Image.open(image_path) as img:
        width, height = img.size

    if width <= 0 or height <= 0:
        return BASE_IMAGE_TOKENS

    scale_2048 = min(2048 / width, 2048 / height, 1.0)
    width_2048 = width * scale_2048
    height_2048 = height * scale_2048

    shortest = min(width_2048, height_2048)
    if shortest <= 0:
        return BASE_IMAGE_TOKENS

    scale_768 = 768 / shortest
    width_final = width_2048 * scale_768
    height_final = height_2048 * scale_768

    tiles = math.ceil(width_final / 512) * math.ceil(height_final / 512)
    return BASE_IMAGE_TOKENS + (HIGH_DETAIL_TILE_TOKENS * tiles)


def _estimate_costs(
    images: list[Path],
    input_per_1m: float,
    output_per_1m: float,
) -> dict:
    total_input_tokens = 0
    for image_path in images:
        total_input_tokens += _estimate_image_input_tokens(image_path)

    count = len(images)
    avg_input_tokens = (total_input_tokens / count) if count else 0

    low_output_total = DEFAULT_OUTPUT_TOKENS_LOW * count
    typical_output_total = DEFAULT_OUTPUT_TOKENS_TYPICAL * count
    high_output_total = DEFAULT_OUTPUT_TOKENS_HIGH * count

    def dollars(input_tokens: int, output_tokens: int) -> float:
        return (input_tokens / 1_000_000) * input_per_1m + (output_tokens / 1_000_000) * output_per_1m

    low_total = dollars(total_input_tokens, low_output_total)
    typical_total = dollars(total_input_tokens, typical_output_total)
    high_total = dollars(total_input_tokens, high_output_total)

    return {
        "images": count,
        "total_input_tokens": total_input_tokens,
        "avg_input_tokens": avg_input_tokens,
        "low_total": low_total,
        "typical_total": typical_total,
        "high_total": high_total,
        "low_per_image": low_total / count if count else 0,
        "typical_per_image": typical_total / count if count else 0,
        "high_per_image": high_total / count if count else 0,
    }


def _print_estimate(summary: dict, input_per_1m: float, output_per_1m: float) -> None:
    print("─" * 60)
    print("Cost estimate (rough)")
    print(f"  Images                 : {summary['images']}")
    print(f"  Avg image input tokens : {summary['avg_input_tokens']:.0f}")
    print(f"  Total image input toks : {summary['total_input_tokens']}")
    print(f"  Pricing used           : input ${input_per_1m:.2f}/1M, output ${output_per_1m:.2f}/1M")
    print("  Output token assumptions per image: low=200, typical=500, high=900")
    print(f"  Estimated per-image    : ${summary['low_per_image']:.4f} - ${summary['high_per_image']:.4f}")
    print(f"  Estimated total        : ${summary['low_total']:.4f} - ${summary['high_total']:.4f}")
    print(f"  Typical total          : ${summary['typical_total']:.4f}")
    print("─" * 60)


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


def _load_existing_collection(output_path: str) -> dict:
    """Load existing output JSON so scan appends by default.

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
        print(f"Warning: could not read existing output JSON, starting fresh: {exc}")
        return {}

    if not isinstance(existing, dict):
        print("Warning: existing output JSON is not an object, starting fresh.")
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


def _create_backup_if_exists(file_path: Path) -> Path | None:
    """Create a timestamped rollback backup if the target file already exists."""
    if not file_path.exists() or not file_path.is_file():
        return None

    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    backup_path = file_path.with_name(f"{file_path.stem}.backup.{timestamp}{file_path.suffix}")
    try:
        shutil.copy2(file_path, backup_path)
    except OSError as exc:
        print(f"Warning: could not create rollback backup for {file_path.name}: {exc}")
        return None

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
            print(f"Warning: could not remove old backup {stale_backup.name}: {exc}")

    return backup_path


def scan(
    image_folder: str,
    output_path: str | None = None,
    approve: bool = False,
    estimate_only: bool = False,
    input_per_1m: float = DEFAULT_INPUT_PER_1M,
    output_per_1m: float = DEFAULT_OUTPUT_PER_1M,
    provider: str = "openai",
    vision_model: str | None = None,
) -> None:
    folder = Path(image_folder)
    if not folder.is_dir():
        print(f"ERROR: '{image_folder}' is not a valid directory.")
        sys.exit(1)

    images = _collect_images(folder)
    if not images:
        print(f"No supported images found in '{image_folder}'.")
        sys.exit(0)

    estimate = _estimate_costs(images, input_per_1m=input_per_1m, output_per_1m=output_per_1m)
    _print_estimate(estimate, input_per_1m=input_per_1m, output_per_1m=output_per_1m)

    if estimate_only:
        print("Estimate-only mode enabled. No API calls were made.")
        return

    if not approve:
        print("Approval required before paid API calls.")
        print("Re-run with --approve to continue scanning.")
        print("No API calls were made.")
        return

    # Default output: cards.json next to the images
    if output_path is None:
        output_path = str(folder / "cards.json")

    output_file = Path(output_path)
    unresolved_output_file = output_file.with_name(f"{output_file.stem}.unresolved{output_file.suffix}")

    print(f"Found {len(images)} image(s) in '{folder.name}'")
    print(f"Vision provider: {provider}  model: {vision_model or '(default)'}")
    print(f"Output will be written to: {output_path}\n")

    collection: dict[str, dict] = _load_existing_collection(output_path)
    if collection:
        existing_total = sum(v.get("count", 0) for v in collection.values())
        print(
            f"Append mode: loaded {len(collection)} existing unique cards "
            f"({existing_total} copies)."
        )

    unresolved: list[dict]       = []  # candidates that couldn't be confirmed

    for i, img_path in enumerate(images, start=1):
        print(f"[{i}/{len(images)}] Processing {img_path.name} …")

        candidates = vision.identify_cards(
            str(img_path),
            provider=provider,
            model=vision_model,
        )

        if not candidates:
            print(f"  No cards identified.\n")
            continue

        for candidate in candidates:
            resolved = scryfall.resolve(candidate)
            if resolved:
                resolved["finish"] = _apply_finish_policy(candidate, resolved)
                _merge(collection, resolved)
            else:
                attempted_methods, failed_after = _unresolved_match_context(candidate)
                unresolved.append({
                    "source_image": img_path.name,
                    "attempted_methods": attempted_methods,
                    "failed_after": failed_after,
                    **candidate,
                })

        print()

    # ── Build final output ────────────────────────────────────────────────────
    output = dict(sorted(collection.items()))  # alphabetical by printing key

    backup_path = _create_backup_if_exists(output_file)
    if backup_path:
        print(f"Rollback backup created: {backup_path.name}")

    with open(output_path, "w", encoding="utf-8") as f:
        json.dump(output, f, indent=2, ensure_ascii=False)

    if unresolved:
        with open(unresolved_output_file, "w", encoding="utf-8") as f:
            json.dump(unresolved, f, indent=2, ensure_ascii=False)

    total_cards  = sum(v["count"] for v in collection.values())
    unique_cards = len(collection)

    print("─" * 60)
    print(f"Done.")
    print(f"  Unique cards identified : {unique_cards}")
    print(f"  Total copies counted    : {total_cards}")
    if unresolved:
        print(f"  Unresolved entries      : {len(unresolved)}")
        print(f"  Unresolved saved to     : {unresolved_output_file}")
    print(f"  Output saved to         : {output_path}")


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Scan MTG binder photos and produce a card inventory JSON."
    )
    parser.add_argument(
        "image_folder",
        help="Path to the folder containing binder photos.",
    )
    parser.add_argument(
        "--output",
        default=None,
        help="Path for the output JSON file. Defaults to cards.json inside the image folder.",
    )
    parser.add_argument(
        "--approve",
        action="store_true",
        help="Proceed with paid vision calls after showing the estimate.",
    )
    parser.add_argument(
        "--estimate-only",
        action="store_true",
        help="Show cost estimate and exit without any API calls.",
    )
    parser.add_argument(
        "--input-per-1m",
        type=float,
        default=DEFAULT_INPUT_PER_1M,
        help="Input token price per 1M tokens (default: 2.50).",
    )
    parser.add_argument(
        "--output-per-1m",
        type=float,
        default=DEFAULT_OUTPUT_PER_1M,
        help="Output token price per 1M tokens (default: 10.00).",
    )
    parser.add_argument(
        "--provider",
        choices=["openai", "gemini"],
        default="openai",
        help="Vision provider to use for image identification.",
    )
    parser.add_argument(
        "--vision-model",
        default=None,
        help="Optional model override for the selected provider.",
    )
    parser.add_argument(
        "--gemini-tier",
        choices=["2.5", "3"],
        default=None,
        help="Gemini model preset shortcut (2.5 -> gemini-2.5-flash, 3 -> gemini-3-flash-preview).",
    )
    args = parser.parse_args()

    selected_model = args.vision_model
    if args.provider == "gemini" and args.gemini_tier and not selected_model:
        selected_model = GEMINI_TIER_MODEL_MAP[args.gemini_tier]

    if args.provider != "gemini" and args.gemini_tier:
        print("Note: --gemini-tier is ignored unless --provider gemini is used.")

    scan(
        args.image_folder,
        args.output,
        approve=args.approve,
        estimate_only=args.estimate_only,
        input_per_1m=args.input_per_1m,
        output_per_1m=args.output_per_1m,
        provider=args.provider,
        vision_model=selected_model,
    )


if __name__ == "__main__":
    main()
