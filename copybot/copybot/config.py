"""Load settings from a TOML file."""

from __future__ import annotations

import json
import tomllib
from dataclasses import dataclass, field, fields
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
    target_leaders: int = 3
    risk: RiskConfig = field(default_factory=RiskConfig)


def load_config(path: str | Path) -> BotConfig:
    with open(path, "rb") as fh:
        raw = tomllib.load(fh)
    bot = raw.get("bot", {})
    paper = raw.get("paper", {})
    leaders = [a.strip().lower() for a in bot.get("leaders", []) if a.strip()]
    validate_leaders(leaders)
    return BotConfig(
        leaders=leaders,
        starting_balance=float(paper.get("starting_balance", 1000.0)),
        copy_ratio=float(bot.get("copy_ratio", 1.0)),
        copy_existing_positions=bool(bot.get("copy_existing_positions", False)),
        poll_seconds=float(bot.get("poll_seconds", 15.0)),
        fee_bps=float(paper.get("fee_bps", 4.5)),
        slippage_bps=float(paper.get("slippage_bps", 2.0)),
        data_dir=str(bot.get("data_dir", "data")),
        target_leaders=int(bot.get("target_leaders", 3)),
        risk=RiskConfig(**raw.get("risk", {})),
    )


def validate_leaders(leaders: list[str]) -> None:
    for addr in leaders:
        if not (addr.startswith("0x") and len(addr) == 42):
            raise ValueError(f"config: not a valid address: {addr}")


OVERRIDES_FILE = "overrides.json"
RISK_KEYS = {f.name for f in fields(RiskConfig)}


def apply_overrides(cfg: BotConfig) -> BotConfig:
    """The self-improvement loop stores its decisions in <data_dir>/overrides.json.
    They take precedence over the TOML file."""
    path = Path(cfg.data_dir) / OVERRIDES_FILE
    if not path.exists():
        return cfg
    data = json.loads(path.read_text())
    if "leaders" in data:
        cfg.leaders = [a.lower() for a in data["leaders"]]
        validate_leaders(cfg.leaders)
    for key, value in data.get("risk", {}).items():
        if key in RISK_KEYS:
            setattr(cfg.risk, key, value)
    return cfg


def load_overrides(data_dir: str | Path) -> dict:
    path = Path(data_dir) / OVERRIDES_FILE
    return json.loads(path.read_text()) if path.exists() else {}


def save_overrides(data_dir: str | Path, data: dict) -> None:
    path = Path(data_dir) / OVERRIDES_FILE
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2) + "\n")
