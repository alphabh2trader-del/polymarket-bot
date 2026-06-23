"""
Polymarket CLOB REST API client.

Docs: https://docs.polymarket.com/#clob-client
Public endpoints require no auth. Trading endpoints require signing.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

import requests
from tenacity import retry, stop_after_attempt, wait_exponential, retry_if_exception_type

from src.utils.logger import get_logger

log = get_logger(__name__)

CLOB_HOST = "https://clob.polymarket.com"
GAMMA_HOST = "https://gamma-api.polymarket.com"  # metadata / market search


@dataclass
class MarketData:
    condition_id: str
    question: str
    description: str
    category: str
    yes_price: float
    no_price: float
    volume_24h: float
    open_interest: float
    resolution_date: datetime | None
    active: bool
    tokens: list[dict] = field(default_factory=list)

    @property
    def implied_yes_prob(self) -> float:
        return self.yes_price

    @property
    def implied_no_prob(self) -> float:
        return self.no_price


@dataclass
class PricePoint:
    timestamp: datetime
    yes_price: float
    no_price: float
    volume: float = 0.0


class PolymarketClient:
    def __init__(self, host: str = CLOB_HOST, api_key: str = "", private_key: str = ""):
        self.host = host.rstrip("/")
        self.api_key = api_key
        self.private_key = private_key
        self._session = requests.Session()
        self._session.headers.update({"Content-Type": "application/json"})

    # ------------------------------------------------------------------ #
    # Internal helpers                                                     #
    # ------------------------------------------------------------------ #

    @retry(
        retry=retry_if_exception_type(requests.RequestException),
        stop=stop_after_attempt(3),
        wait=wait_exponential(multiplier=1, min=2, max=30),
        reraise=True,
    )
    def _get(self, path: str, params: dict | None = None) -> Any:
        url = f"{self.host}{path}"
        resp = self._session.get(url, params=params, timeout=15)
        resp.raise_for_status()
        return resp.json()

    @retry(
        retry=retry_if_exception_type(requests.RequestException),
        stop=stop_after_attempt(3),
        wait=wait_exponential(multiplier=1, min=2, max=30),
        reraise=True,
    )
    def _get_gamma(self, path: str, params: dict | None = None) -> Any:
        url = f"{GAMMA_HOST}{path}"
        resp = self._session.get(url, params=params, timeout=15)
        resp.raise_for_status()
        return resp.json()

    # ------------------------------------------------------------------ #
    # Market listing                                                       #
    # ------------------------------------------------------------------ #

    def get_markets(self, limit: int = 100, offset: int = 0) -> list[MarketData]:
        """Fetch active markets from Gamma (metadata API)."""
        try:
            data = self._get_gamma(
                "/markets",
                params={
                    "active": "true",
                    "closed": "false",
                    "limit": limit,
                    "offset": offset,
                },
            )
        except Exception as exc:
            log.error(f"Failed to fetch markets: {exc}")
            return []

        markets = []
        items = data if isinstance(data, list) else data.get("data", data.get("markets", []))
        for item in items:
            m = self._parse_gamma_market(item)
            if m:
                markets.append(m)
        return markets

    def get_all_active_markets(self, batch_size: int = 100) -> list[MarketData]:
        """Paginate through all active markets."""
        all_markets: list[MarketData] = []
        offset = 0
        while True:
            batch = self.get_markets(limit=batch_size, offset=offset)
            if not batch:
                break
            all_markets.extend(batch)
            if len(batch) < batch_size:
                break
            offset += batch_size
            time.sleep(0.3)  # polite pacing
        log.info(f"Fetched {len(all_markets)} active markets total")
        return all_markets

    def get_market(self, condition_id: str) -> MarketData | None:
        """Fetch a single market by condition ID."""
        try:
            data = self._get_gamma(f"/markets/{condition_id}")
            return self._parse_gamma_market(data)
        except Exception as exc:
            log.error(f"Failed to fetch market {condition_id}: {exc}")
            return None

    # ------------------------------------------------------------------ #
    # Price data                                                           #
    # ------------------------------------------------------------------ #

    def get_price_history(
        self,
        token_id: str,
        interval: str = "1h",
        fidelity: int = 60,
    ) -> list[PricePoint]:
        """
        Fetch CLOB time-series prices for a token.

        interval: 1m, 5m, 1h, 6h, 1d, 1w, all
        fidelity: number of data points to return
        """
        try:
            data = self._get(
                f"/prices-history",
                params={"market": token_id, "interval": interval, "fidelity": fidelity},
            )
        except Exception as exc:
            log.warning(f"Price history unavailable for {token_id}: {exc}")
            return []

        history: list[PricePoint] = []
        points = data.get("history", [])
        for p in points:
            try:
                ts = datetime.fromtimestamp(p["t"])
                price = float(p.get("p", 0))
                history.append(PricePoint(timestamp=ts, yes_price=price, no_price=1 - price))
            except (KeyError, ValueError):
                continue
        return history

    def get_orderbook(self, token_id: str) -> dict:
        """Fetch current order book for a token."""
        try:
            return self._get(f"/book", params={"token_id": token_id})
        except Exception as exc:
            log.warning(f"Orderbook unavailable for {token_id}: {exc}")
            return {}

    # ------------------------------------------------------------------ #
    # Parsing helpers                                                      #
    # ------------------------------------------------------------------ #

    def _parse_gamma_market(self, item: dict) -> MarketData | None:
        import json as _json
        try:
            condition_id = item.get("conditionId") or item.get("condition_id", "")
            if not condition_id:
                return None

            # outcomePrices is a JSON-encoded string: "[\"0.515\", \"0.485\"]"
            raw_prices = item.get("outcomePrices", "")
            if isinstance(raw_prices, str) and raw_prices:
                try:
                    prices_list = _json.loads(raw_prices)
                    yes_price = float(prices_list[0]) if prices_list else 0.5
                except (ValueError, IndexError):
                    yes_price = 0.5
            elif isinstance(raw_prices, list) and raw_prices:
                yes_price = float(raw_prices[0])
            else:
                # Fall back to lastTradePrice or bestBid
                yes_price = float(item.get("lastTradePrice") or item.get("bestBid") or 0.5)
            no_price = round(1.0 - yes_price, 6)

            # clobTokenIds is also a JSON-encoded string
            raw_tokens = item.get("clobTokenIds", "[]")
            if isinstance(raw_tokens, str):
                try:
                    tokens = _json.loads(raw_tokens)
                except ValueError:
                    tokens = []
            else:
                tokens = raw_tokens if isinstance(raw_tokens, list) else []

            # Category from events[0].title or groupItemTitle
            category = ""
            events = item.get("events", [])
            if events and isinstance(events, list):
                category = events[0].get("title", "")[:50]
            if not category:
                category = item.get("groupItemTitle", item.get("category", ""))

            # Resolution date
            end_date_iso = item.get("endDate") or item.get("endDateIso") or item.get("end_date")
            resolution_date: datetime | None = None
            if end_date_iso:
                try:
                    resolution_date = datetime.fromisoformat(str(end_date_iso).replace("Z", "+00:00"))
                except ValueError:
                    pass

            # Open interest from events if not at top level
            open_interest = float(item.get("openInterest", 0) or 0)
            if open_interest == 0 and events:
                open_interest = float(events[0].get("openInterest", 0) or 0)

            return MarketData(
                condition_id=condition_id,
                question=item.get("question", ""),
                description=item.get("description", ""),
                category=category,
                yes_price=yes_price,
                no_price=no_price,
                volume_24h=float(item.get("volume24hr", item.get("volumeNum", item.get("volume", 0))) or 0),
                open_interest=open_interest,
                resolution_date=resolution_date,
                active=bool(item.get("active", True)),
                tokens=tokens,
            )
        except Exception as exc:
            log.debug(f"Failed to parse market item: {exc} | data={item}")
            return None
