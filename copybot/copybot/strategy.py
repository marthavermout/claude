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


def fair_entry(snapshot: LeaderSnapshot, coin: str, mids: dict[str, float] | None, max_drift: float) -> bool:
    """True if joining now costs at most `max_drift` more than the leader paid.
    Long: price may not be more than max_drift above their entry. Short: not more than
    max_drift below it. A better price than theirs is always fine."""
    if not mids or max_drift <= 0:
        return False
    entry, mid, size = snapshot.entries.get(coin), mids.get(coin), snapshot.positions.get(coin, 0.0)
    if not entry or not mid or not size:
        return False
    worse_by = (mid / entry - 1) if size > 0 else (1 - mid / entry)
    return worse_by <= max_drift


def filter_baseline(snapshot: LeaderSnapshot, baseline: dict[str, int],
                    mids: dict[str, float] | None = None, max_drift: float = 0.0) -> LeaderSnapshot:
    """Hide positions the leader already had when we started (we'd enter at a worse
    price than they did). A coin leaves the baseline once the leader closes or flips it,
    or as soon as the price is back within `max_drift` of the leader's own entry.
    `baseline` maps coin -> sign (+1/-1) and is updated in place."""
    visible = {}
    for coin in list(baseline):
        size = snapshot.positions.get(coin, 0.0)
        if size == 0 or (size > 0) != (baseline[coin] > 0) or fair_entry(snapshot, coin, mids, max_drift):
            del baseline[coin]
    for coin, size in snapshot.positions.items():
        if coin not in baseline:
            visible[coin] = size
    return LeaderSnapshot(address=snapshot.address, equity=snapshot.equity, positions=visible, entries=snapshot.entries)
