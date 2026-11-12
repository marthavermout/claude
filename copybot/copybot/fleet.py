"""Run all bots in one process, so they share one view of the market.

Before, each of the 18 bots asked Hyperliquid for the same traders on its own (every
30-60 s), which wasted the exchange's rate limit. Now one shared, cached client fetches
each trader once per round for everyone, which leaves room to look every few seconds.

On top of that, a websocket listens for the followed traders' own fills. When one of them
trades, the bots are woken immediately (about 1-2 s) instead of waiting for the next round.
Hyperliquid lets one connection follow about 10 traders, so the websocket watches the 10
most active ones (measured live: median 0.4 s from their trade to our wake-up) and the
rest are polled every few seconds. If the websocket is unavailable the bots keep polling.
"""

from __future__ import annotations

import json
import logging
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone

from .config import BotConfig
from .engine import Engine
from .hyperliquid import ApiError, HyperliquidClient, LeaderSnapshot

log = logging.getLogger("copybot")


class SharedClient:
    """Caches exchange answers so many bots in the same round cost one request each.

    `stale(address)` marks a trader as changed (the websocket saw a fill), so the next
    round fetches that trader again even if the cached copy is still young."""

    def __init__(self, inner: HyperliquidClient | None = None, snapshot_ttl: float = 4.0,
                 slow_ttl: float = 55.0, clock=time.monotonic):
        self.inner = inner or HyperliquidClient()
        self.snapshot_ttl, self.slow_ttl, self.clock = snapshot_ttl, slow_ttl, clock
        self._cache: dict[tuple, tuple[float, object]] = {}
        self._dirty: set[str] = set()
        self._lock = threading.Lock()
        self.requests = 0

    def _get(self, key: tuple, ttl: float, fetch):
        now = self.clock()
        with self._lock:
            hit = self._cache.get(key)
            dirty = key[0] == "snap" and key[1] in self._dirty
        if hit and now - hit[0] < ttl and not dirty:
            return hit[1]
        value = fetch()
        self.requests += 1
        with self._lock:
            self._cache[key] = (now, value)
            if key[0] == "snap":
                self._dirty.discard(key[1])
        return value

    def stale(self, address: str) -> None:
        with self._lock:
            self._dirty.add(address.lower())

    def new_round(self) -> None:
        """Mids are always fetched fresh once per round."""
        with self._lock:
            self._cache.pop(("mids",), None)

    def prefetch(self, addresses: list[str], workers: int = 8) -> None:
        """Fetch prices and every trader in parallel at the start of a round, so the bots
        only read from the cache. Errors are left for the bot that needs the data."""
        def one(fn):
            try:
                fn()
            except Exception:
                pass
        with ThreadPoolExecutor(max_workers=workers) as pool:
            list(pool.map(one, [self.mids] + [lambda a=a: self.snapshot(a) for a in addresses]))

    def mids(self):
        return self._get(("mids",), 10_000, self.inner.mids)

    def snapshot(self, address: str) -> LeaderSnapshot:
        return self._get(("snap", address.lower()), self.snapshot_ttl, lambda: self.inner.snapshot(address))

    def size_decimals(self):
        return self._get(("dec",), 3600, self.inner.size_decimals)

    def asset_ctxs(self):
        return self._get(("ctx",), self.slow_ttl, self.inner.asset_ctxs)

    def candles(self, coin: str, interval: str = "15m", count: int = 250):
        return self._get(("candles", coin, interval, count), self.slow_ttl, lambda: self.inner.candles(coin, interval, count))


MAX_WS_TRADERS = 10  # Hyperliquid's limit on traders followed per websocket connection


def most_active(client: HyperliquidClient, addresses: list[str], n: int = MAX_WS_TRADERS) -> list[str]:
    """The n traders with the most fills in the last 24 h (they gain most from instant copying)."""
    counts = {}
    for a in addresses:
        try:
            counts[a] = len(client.recent_fills(a, 24))
        except Exception:
            counts[a] = 0
    return sorted(addresses, key=lambda a: -counts[a])[:n]


class FillWatcher(threading.Thread):
    """Listens on Hyperliquid's websocket for new fills of the followed traders."""

    URL = "wss://api.hyperliquid.xyz/ws"

    def __init__(self, addresses: list[str], on_fill):
        super().__init__(daemon=True)
        self.addresses = [a.lower() for a in addresses][:MAX_WS_TRADERS]
        self.on_fill = on_fill
        self.connected = False
        self.events = 0
        self._stop = threading.Event()

    def stop(self) -> None:
        self._stop.set()

    def run(self) -> None:
        try:
            from websockets.sync.client import connect
        except ImportError:
            log.warning("websockets not installed: polling only")
            return
        backoff = 2
        while not self._stop.is_set():
            try:
                with connect(self.URL, open_timeout=15, ping_interval=20) as ws:
                    for a in self.addresses:
                        ws.send(json.dumps({"method": "subscribe", "subscription": {"type": "userFills", "user": a}}))
                    self.connected, backoff = True, 2
                    log.info("websocket: listening to %d traders", len(self.addresses))
                    last_ping = time.monotonic()
                    while not self._stop.is_set():
                        if time.monotonic() - last_ping > 30:  # Hyperliquid closes a socket after 60 s without a client message
                            ws.send(json.dumps({"method": "ping"}))
                            last_ping = time.monotonic()
                        try:
                            raw = ws.recv(timeout=5)
                        except TimeoutError:
                            continue
                        msg = json.loads(raw)
                        data = msg.get("data") or {}
                        # the first message per trader is a snapshot of old fills: ignore it
                        if msg.get("channel") == "userFills" and not data.get("isSnapshot") and data.get("fills"):
                            self.events += 1
                            self.on_fill(str(data.get("user", "")).lower())
            except Exception as exc:  # network hiccup: retry, polling keeps working meanwhile
                self.connected = False
                log.warning("websocket down (%s), retrying in %ss", exc, backoff)
                self._stop.wait(backoff)
                backoff = min(backoff * 2, 60)


def run_fleet(bots: list[tuple[str, BotConfig]], minutes: float, poll_seconds: float = 5.0,
              min_gap: float = 2.0, use_websocket: bool = True, log_every_s: float = 30.0) -> dict:
    """Run every bot on one shared client until `minutes` have passed. Returns run stats.
    Budget: ~1 price request per round plus each trader once per `poll_seconds`, i.e. about
    600 of Hyperliquid's 1200 weight per minute with 25 traders, even during bursts of fills."""
    shared = SharedClient(snapshot_ttl=poll_seconds - 0.5)
    engines: list[tuple[str, Engine]] = []
    for name, cfg in bots:
        eng = Engine(cfg, client=shared)
        eng.log_every_s = log_every_s
        engines.append((name, eng))
    wake = threading.Event()

    def on_fill(address: str) -> None:
        shared.stale(address)
        wake.set()

    leaders = sorted({a for _, e in engines for a in e.cfg.leaders})
    watcher = FillWatcher(most_active(shared.inner, leaders), on_fill) if use_websocket and leaders else None
    if watcher:
        watcher.start()
    log.info("fleet: %d bots, %d unique traders, polling every %.0fs%s", len(engines), len(leaders),
             poll_seconds, f", websocket on the {len(watcher.addresses)} most active" if watcher else "")
    deadline = time.monotonic() + minutes * 60
    rounds = early = fills = 0
    last = 0.0
    try:
        while time.monotonic() < deadline:
            woke = wake.wait(timeout=max(0.0, poll_seconds - (time.monotonic() - last)))
            wake.clear()
            gap = time.monotonic() - last
            if gap < min_gap:  # never hammer the exchange, even during a burst of fills
                time.sleep(min_gap - gap)
            last = time.monotonic()
            rounds += 1
            early += bool(woke)
            shared.new_round()
            shared.prefetch(leaders)
            now = datetime.now(timezone.utc)
            for name, eng in engines:
                try:
                    fills += len(eng.tick(now))
                except ApiError as exc:
                    log.warning("%s: data unavailable this round: %s", name, exc)
                except Exception:  # one broken bot must not stop the others
                    log.exception("%s: unexpected error", name)
    finally:
        if watcher:
            watcher.stop()
        for name, eng in engines:
            try:
                eng.flush_replay()
                eng.save()
            except Exception:
                log.exception("%s: could not save", name)
    stats = {"rounds": rounds, "woken_by_trades": early, "fills": fills, "requests": shared.requests,
             "websocket_events": watcher.events if watcher else 0}
    log.info("fleet finished: %s", stats)
    return stats
