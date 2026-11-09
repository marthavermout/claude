"""Load settings from a TOML file."""

from __future__ import annotations

import tomllib
from dataclasses import dataclass, field
from pathlib import Path

from .risk import RiskConfig


@dataclass
class BotConfig:
    leaders: list[str]
    starting_balance: float = 1000.0
    copy_ratio: float = 1.0
    copy_existing_positions: bool = False
    poll_seconds: float = 15.0
    fee_bps: float = 4.5
    slippage_bps: float = 2.0
    data_dir: str = "data"
    risk: RiskConfig = field(default_factory=RiskConfig)


def load_config(path: str | Path) -> BotConfig:
    with open(path, "rb") as fh:
        raw = tomllib.load(fh)
    bot = raw.get("bot", {})
    paper = raw.get("paper", {})
    leaders = [a.strip().lower() for a in bot.get("leaders", []) if a.strip()]
    if not leaders:
        raise ValueError("config: [bot].leaders is empty; run `python -m copybot select` first")
    for addr in leaders:
        if not (addr.startswith("0x") and len(addr) == 42):
            raise ValueError(f"config: not a valid address: {addr}")
    return BotConfig(
        leaders=leaders,
        starting_balance=float(paper.get("starting_balance", 1000.0)),
        copy_ratio=float(bot.get("copy_ratio", 1.0)),
        copy_existing_positions=bool(bot.get("copy_existing_positions", False)),
        poll_seconds=float(bot.get("poll_seconds", 15.0)),
        fee_bps=float(paper.get("fee_bps", 4.5)),
        slippage_bps=float(paper.get("slippage_bps", 2.0)),
        data_dir=str(bot.get("data_dir", "data")),
        risk=RiskConfig(**raw.get("risk", {})),
    )
