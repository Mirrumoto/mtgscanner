"""
pricing.py — Pricing abstraction layer for scanner + GUI.

Primary behavior:
- Resolve cards/print options from Scryfall (identity + imagery)
- Enrich prices from MTGJSON when configured
- Fallback to Scryfall prices when MTGJSON is unavailable
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import scryfall
from mtgjson_prices import MTGJSONPriceIndex, PricePolicy


@dataclass(frozen=True)
class PricingConfig:
    source: str = "mtgjson"   # mtgjson | scryfall
    provider: str = "tcgplayer"
    side: str = "retail"      # retail | buylist
    fallback_to_scryfall: bool = True


def normalize_finish(raw_finish: str | None) -> str:
    value = str(raw_finish or "unknown").strip().lower()
    if value in {"nonfoil", "non-foil"}:
        return "nonfoil"
    if value in {"foil", "etched"}:
        return value
    return "unknown"


def price_from_prices_dict(prices: dict, finish: str = "unknown") -> float:
    if not isinstance(prices, dict):
        return 0.0

    normalized_finish = normalize_finish(finish)
    if normalized_finish == "foil":
        candidates = [prices.get("usd_foil"), prices.get("usd")]
    elif normalized_finish == "etched":
        candidates = [prices.get("usd_etched"), prices.get("usd_foil"), prices.get("usd")]
    else:
        candidates = [prices.get("usd"), prices.get("usd_foil"), prices.get("usd_etched")]

    for value in candidates:
        try:
            if value is None:
                continue
            return float(value)
        except (TypeError, ValueError):
            continue
    return 0.0


class PricingService:
    def __init__(self, app_data_dir: Path):
        self.index = MTGJSONPriceIndex(app_data_dir)

    def _build_prices_payload(self, price: float | None, finish: str, source_label: str, provider: str, side: str) -> dict:
        prices: dict[str, object] = {
            "_source": source_label,
            "_provider": provider,
            "_side": side,
        }
        if price is None:
            return prices

        normalized_finish = normalize_finish(finish)
        numeric = float(price)
        if normalized_finish == "foil":
            prices["usd_foil"] = numeric
            prices["usd"] = numeric
        elif normalized_finish == "etched":
            prices["usd_etched"] = numeric
            prices["usd"] = numeric
        else:
            prices["usd"] = numeric
        return prices

    def _try_mtgjson_lookup(self, card: dict, config: PricingConfig, finish: str) -> tuple[dict | None, str | None]:
        policy = PricePolicy(provider=config.provider, side=config.side)
        price, uuid = self.index.lookup_price(
            set_code=str(card.get("set") or ""),
            collector_number=str(card.get("collector_number") or ""),
            finish=finish,
            scryfall_id=str(card.get("id") or "") or None,
            policy=policy,
        )
        if price is None:
            return None, uuid
        return self._build_prices_payload(
            price=price,
            finish=finish,
            source_label="mtgjson",
            provider=config.provider,
            side=config.side,
        ), uuid

    def _lookup_scryfall_print_prices(
        self,
        *,
        name: str,
        set_code: str,
        collector_number: str,
        finish: str,
    ) -> dict | None:
        options = scryfall.get_print_options(name)
        normalized_set = str(set_code or "").strip().upper()
        normalized_number = str(collector_number or "").strip()
        normalized_finish = normalize_finish(finish)

        # Pass 1: exact set + collector number + finish
        for option in options:
            if str(option.get("set", "")).upper() != normalized_set:
                continue
            if str(option.get("collector_number", "")).strip() != normalized_number:
                continue
            if normalize_finish(option.get("finish")) != normalized_finish:
                continue
            prices = option.get("prices") if isinstance(option.get("prices"), dict) else {}
            prices = dict(prices)
            prices.setdefault("_source", "scryfall")
            return prices

        # Pass 2: exact set + collector number regardless of finish
        for option in options:
            if str(option.get("set", "")).upper() != normalized_set:
                continue
            if str(option.get("collector_number", "")).strip() != normalized_number:
                continue
            prices = option.get("prices") if isinstance(option.get("prices"), dict) else {}
            prices = dict(prices)
            prices.setdefault("_source", "scryfall")
            return prices

        return None

    def resolve(self, candidate: dict, config: PricingConfig) -> dict | None:
        card = scryfall.resolve(candidate)
        if not card:
            return None

        finish = normalize_finish(candidate.get("finish"))

        if config.source == "scryfall":
            card["prices"] = card.get("prices") if isinstance(card.get("prices"), dict) else {}
            return card

        mtgjson_prices = None
        mtgjson_uuid = None
        try:
            mtgjson_prices, mtgjson_uuid = self._try_mtgjson_lookup(card, config, finish)
        except Exception:
            mtgjson_prices = None
            mtgjson_uuid = None

        if mtgjson_prices is not None:
            card["prices"] = mtgjson_prices
            if mtgjson_uuid:
                card["mtgjson_uuid"] = mtgjson_uuid
            return card

        if config.fallback_to_scryfall:
            existing = card.get("prices") if isinstance(card.get("prices"), dict) else {}
            existing.setdefault("_source", "scryfall")
            card["prices"] = existing
            if mtgjson_uuid:
                card["mtgjson_uuid"] = mtgjson_uuid
            return card

        card["prices"] = self._build_prices_payload(
            price=None,
            finish=finish,
            source_label="mtgjson-unavailable",
            provider=config.provider,
            side=config.side,
        )
        if mtgjson_uuid:
            card["mtgjson_uuid"] = mtgjson_uuid
        return card

    def get_print_options(self, name: str, config: PricingConfig) -> list[dict]:
        options = scryfall.get_print_options(name)
        if config.source == "scryfall":
            return options

        enriched: list[dict] = []
        for option in options:
            finish = normalize_finish(option.get("finish"))
            mtgjson_prices = None
            mtgjson_uuid = None

            try:
                policy = PricePolicy(provider=config.provider, side=config.side)
                price, mtgjson_uuid = self.index.lookup_price(
                    set_code=str(option.get("set") or ""),
                    collector_number=str(option.get("collector_number") or ""),
                    finish=finish,
                    scryfall_id=None,
                    policy=policy,
                )
                if price is not None:
                    mtgjson_prices = self._build_prices_payload(
                        price=price,
                        finish=finish,
                        source_label="mtgjson",
                        provider=config.provider,
                        side=config.side,
                    )
            except Exception:
                mtgjson_prices = None

            cloned = dict(option)
            if mtgjson_prices is not None:
                cloned["prices"] = mtgjson_prices
            elif config.fallback_to_scryfall:
                original_prices = cloned.get("prices") if isinstance(cloned.get("prices"), dict) else {}
                original_prices.setdefault("_source", "scryfall")
                cloned["prices"] = original_prices
            else:
                cloned["prices"] = self._build_prices_payload(
                    price=None,
                    finish=finish,
                    source_label="mtgjson-unavailable",
                    provider=config.provider,
                    side=config.side,
                )

            if mtgjson_uuid:
                cloned["mtgjson_uuid"] = mtgjson_uuid

            enriched.append(cloned)

        return enriched

    def get_price_for_print(
        self,
        *,
        name: str,
        set_code: str,
        collector_number: str,
        finish: str,
        config: PricingConfig,
        scryfall_id: str | None = None,
    ) -> tuple[dict, float, str | None]:
        normalized_finish = normalize_finish(finish)

        if config.source == "scryfall":
            scryfall_prices = self._lookup_scryfall_print_prices(
                name=name,
                set_code=set_code,
                collector_number=collector_number,
                finish=normalized_finish,
            ) or {}
            return scryfall_prices, price_from_prices_dict(scryfall_prices, normalized_finish), None

        policy = PricePolicy(provider=config.provider, side=config.side)
        try:
            mtgjson_price, mtgjson_uuid = self.index.lookup_price(
                set_code=str(set_code or ""),
                collector_number=str(collector_number or ""),
                finish=normalized_finish,
                scryfall_id=scryfall_id,
                policy=policy,
            )
        except Exception:
            mtgjson_price, mtgjson_uuid = None, None

        if mtgjson_price is not None:
            payload = self._build_prices_payload(
                price=mtgjson_price,
                finish=normalized_finish,
                source_label="mtgjson",
                provider=config.provider,
                side=config.side,
            )
            return payload, float(mtgjson_price), mtgjson_uuid

        if config.fallback_to_scryfall:
            scryfall_prices = self._lookup_scryfall_print_prices(
                name=name,
                set_code=set_code,
                collector_number=collector_number,
                finish=normalized_finish,
            ) or {}
            return scryfall_prices, price_from_prices_dict(scryfall_prices, normalized_finish), mtgjson_uuid

        payload = self._build_prices_payload(
            price=None,
            finish=normalized_finish,
            source_label="mtgjson-unavailable",
            provider=config.provider,
            side=config.side,
        )
        return payload, 0.0, mtgjson_uuid

    def resolve_mtgjson_uuid(
        self,
        *,
        set_code: str,
        collector_number: str,
        finish: str,
        scryfall_id: str | None = None,
    ) -> str | None:
        try:
            return self.index.resolve_uuid(
                set_code=set_code,
                collector_number=collector_number,
                finish=finish,
                scryfall_id=scryfall_id,
            )
        except Exception:
            return None
