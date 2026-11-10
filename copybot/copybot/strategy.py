"""Turn leader positions into our target positions (proportional copying)."""

from __future__ import annotations

from .hyperliquid import LeaderSnapshot


def target_notionals(
    leaders: list[LeaderSnapshot],
    mids: dict[str, float],
    our_equity: float,
    copy_ratio: float = 1.0,
) -> dict[str, float]:
    """Each leader gets an equal slice of our equity. Within that slice we take the
    same exposure *as a percentage of equity* as the leader. Example: the leader has
    $100k and is $20k long BTC (20%). Our slice is $500, so we go $100 long BTC."""
    usable = [ldr for ldr in leaders if ldr.equity > 0]
    if not usable or our_equity <= 0:
        return {}
    slice_equity = our_equity * copy_ratio / len(usable)
    targets: dict[str, float] = {}
    for ldr in usable:
        for coin, size in ldr.positions.items():
            mid = mids.get(coin)
            if mid is None:
                continue
            fraction = size * mid / ldr.equity
            targets[coin] = targets.get(coin, 0.0) + fraction * slice_equity
    return targets


def filter_baseline(snapshot: LeaderSnapshot, baseline: dict[str, int]) -> LeaderSnapshot:
    """Hide positions the leader already had when we started (we'd enter at a worse
    price than they did). A coin leaves the baseline once the leader closes or flips it.
    `baseline` maps coin -> sign (+1/-1) and is updated in place."""
    visible = {}
    for coin in list(baseline):
        size = snapshot.positions.get(coin, 0.0)
        if size == 0 or (size > 0) != (baseline[coin] > 0):
            del baseline[coin]
    for coin, size in snapshot.positions.items():
        if coin not in baseline:
            visible[coin] = size
    return LeaderSnapshot(address=snapshot.address, equity=snapshot.equity, positions=visible)
