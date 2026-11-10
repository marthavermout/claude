"""Risk limits. These run before every trade and can override the leader."""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class RiskConfig:
    max_leverage: float = 2.0  # total position size / equity
    max_position_pct: float = 0.25  # max size of one coin / equity
    daily_loss_limit_pct: float = 0.05  # halt for the rest of the UTC day
    max_drawdown_pct: float = 0.20  # halt permanently (manual reset)
    min_trade_usd: float = 15.0  # ignore tiny adjustments (saves fees)
    allowed_coins: list[str] = field(default_factory=list)  # empty = all
    blocked_coins: list[str] = field(default_factory=list)


@dataclass
class RiskState:
    day: str = ""
    day_start_equity: float = 0.0
    peak_equity: float = 0.0
    killed: bool = False
    kill_reason: str = ""


class RiskManager:
    def __init__(self, cfg: RiskConfig, state: RiskState | None = None):
        self.cfg = cfg
        self.state = state or RiskState()

    def halt_reason(self, equity: float, today: str) -> str | None:
        """Update counters and return why trading must stop, or None."""
        s = self.state
        if s.day != today:
            s.day = today
            s.day_start_equity = equity
        s.peak_equity = max(s.peak_equity, equity)

        if s.killed:
            return s.kill_reason
        if s.peak_equity > 0 and equity <= s.peak_equity * (1 - self.cfg.max_drawdown_pct):
            s.killed = True
            s.kill_reason = (
                f"max drawdown hit ({self.cfg.max_drawdown_pct:.0%} below peak {s.peak_equity:.2f}); "
                "bot stopped, run `reset-kill` to resume"
            )
            return s.kill_reason
        if s.day_start_equity > 0 and equity <= s.day_start_equity * (1 - self.cfg.daily_loss_limit_pct):
            return f"daily loss limit hit ({self.cfg.daily_loss_limit_pct:.0%}); paused until next UTC day"
        return None

    def coin_allowed(self, coin: str) -> bool:
        if coin in self.cfg.blocked_coins:
            return False
        return not self.cfg.allowed_coins or coin in self.cfg.allowed_coins

    def apply_limits(self, targets: dict[str, float], equity: float) -> dict[str, float]:
        """Cap signed notional targets (USD) per coin and in total."""
        if equity <= 0:
            return {}
        cap = equity * self.cfg.max_position_pct
        out = {c: max(-cap, min(cap, n)) for c, n in targets.items() if self.coin_allowed(c) and n}
        gross = sum(abs(n) for n in out.values())
        max_gross = equity * self.cfg.max_leverage
        if gross > max_gross:
            scale = max_gross / gross
            out = {c: n * scale for c, n in out.items()}
        return out
