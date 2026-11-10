"""Minimal read-only client for the public Hyperliquid info API (no keys needed)."""

from __future__ import annotations

import time
from dataclasses import dataclass, field

import requests

INFO_URL = "https://api.hyperliquid.xyz/info"
LEADERBOARD_URL = "https://stats-data.hyperliquid.xyz/Mainnet/leaderboard"


class ApiError(RuntimeError):
    pass


@dataclass
class LeaderSnapshot:
    address: str
    equity: float
    positions: dict[str, float] = field(default_factory=dict)  # coin -> signed size


class HyperliquidClient:
    def __init__(self, info_url: str = INFO_URL, timeout: float = 10.0, retries: int = 3, session=None):
        self.info_url = info_url
        self.timeout = timeout
        self.retries = retries
        self.session = session or requests.Session()

    def _request(self, method: str, url: str, **kwargs):
        last = None
        for attempt in range(self.retries):
            try:
                resp = self.session.request(method, url, timeout=self.timeout, **kwargs)
                resp.raise_for_status()
                return resp.json()
            except (requests.RequestException, ValueError) as exc:
                last = exc
                time.sleep(2**attempt)
        raise ApiError(f"{method} {url} failed after {self.retries} attempts: {last}")

    def _info(self, payload: dict):
        return self._request("POST", self.info_url, json=payload)

    def mids(self) -> dict[str, float]:
        """Mid prices for perpetuals (spot/outcome keys like '@1' or '#1' are skipped)."""
        raw = self._info({"type": "allMids"})
        return {k: float(v) for k, v in raw.items() if not k.startswith(("@", "#"))}

    def size_decimals(self) -> dict[str, int]:
        raw = self._info({"type": "meta"})
        return {a["name"]: int(a["szDecimals"]) for a in raw["universe"] if not a.get("isDelisted")}

    def snapshot(self, address: str) -> LeaderSnapshot:
        raw = self._info({"type": "clearinghouseState", "user": address})
        equity = float(raw["marginSummary"]["accountValue"])
        positions = {}
        for item in raw.get("assetPositions", []):
            pos = item["position"]
            size = float(pos["szi"])
            if size:
                positions[pos["coin"]] = size
        return LeaderSnapshot(address=address, equity=equity, positions=positions)

    def recent_fills(self, address: str, hours: float = 24) -> list[dict]:
        start = int((time.time() - hours * 3600) * 1000)
        return self._info({"type": "userFillsByTime", "user": address, "startTime": start})

    def leaderboard(self, url: str = LEADERBOARD_URL) -> list[dict]:
        return self._request("GET", url)["leaderboardRows"]
