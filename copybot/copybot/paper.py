"""Simulated broker: fills at mid price plus slippage, charges taker fees."""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class Position:
    size: float = 0.0  # signed: >0 long, <0 short
    entry: float = 0.0


@dataclass
class Fill:
    coin: str
    size: float  # signed delta
    price: float
    fee: float
    realized: float


@dataclass
class PaperBroker:
    starting_balance: float
    fee_bps: float = 4.5
    slippage_bps: float = 2.0
    realized_pnl: float = 0.0
    fees_paid: float = 0.0
    positions: dict[str, Position] = field(default_factory=dict)

    @property
    def cash(self) -> float:
        return self.starting_balance + self.realized_pnl - self.fees_paid

    def size(self, coin: str) -> float:
        pos = self.positions.get(coin)
        return pos.size if pos else 0.0

    def unrealized(self, mids: dict[str, float]) -> float:
        total = 0.0
        for coin, pos in self.positions.items():
            px = mids.get(coin, pos.entry)
            total += (px - pos.entry) * pos.size
        return total

    def equity(self, mids: dict[str, float]) -> float:
        return self.cash + self.unrealized(mids)

    def gross_notional(self, mids: dict[str, float]) -> float:
        return sum(abs(p.size) * mids.get(c, p.entry) for c, p in self.positions.items())

    def execute(self, coin: str, delta: float, mid: float) -> Fill:
        if delta == 0:
            raise ValueError("delta must be non-zero")
        slip = mid * self.slippage_bps / 10_000
        price = mid + slip if delta > 0 else mid - slip
        fee = abs(delta) * price * self.fee_bps / 10_000

        pos = self.positions.setdefault(coin, Position())
        realized = 0.0
        if pos.size == 0 or (pos.size > 0) == (delta > 0):
            new_size = pos.size + delta
            pos.entry = (pos.entry * pos.size + price * delta) / new_size
            pos.size = new_size
        else:
            closing = min(abs(delta), abs(pos.size))
            direction = 1 if pos.size > 0 else -1
            realized = (price - pos.entry) * closing * direction
            remainder = abs(delta) - closing
            pos.size += delta
            if abs(pos.size) < 1e-12:
                pos.size = 0.0
            if remainder > 1e-12:  # position flipped
                pos.entry = price

        if pos.size == 0:
            del self.positions[coin]

        self.realized_pnl += realized
        self.fees_paid += fee
        return Fill(coin=coin, size=delta, price=price, fee=fee, realized=realized)

    def to_dict(self) -> dict:
        return {
            "starting_balance": self.starting_balance,
            "realized_pnl": self.realized_pnl,
            "fees_paid": self.fees_paid,
            "positions": {c: {"size": p.size, "entry": p.entry} for c, p in self.positions.items()},
        }

    @classmethod
    def from_dict(cls, data: dict, fee_bps: float, slippage_bps: float) -> "PaperBroker":
        broker = cls(
            starting_balance=data["starting_balance"],
            fee_bps=fee_bps,
            slippage_bps=slippage_bps,
            realized_pnl=data["realized_pnl"],
            fees_paid=data["fees_paid"],
        )
        broker.positions = {c: Position(**p) for c, p in data["positions"].items()}
        return broker
