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


def fit_small_account(targets: dict[str, float], equity: float, min_order: float, max_coins: int = 0,
                      round_up: bool = False, max_gross: float | None = None) -> dict[str, float]:
    """Make copy targets usable for a small account, where most would fall under the exchange's
    minimum order and be skipped.
    - max_coins: keep only the N largest targets and give them the money of the dropped ones
      (same total exposure, fewer but tradable positions).
    - round_up: a target between half the minimum and the minimum becomes the minimum.
    Total exposure never goes above `max_gross` (the bot's leverage limit)."""
    out = {c: v for c, v in targets.items() if v}
    if not out:
        return out
    gross = sum(abs(v) for v in out.values())
    if max_coins and len(out) > max_coins:
        keep = dict(sorted(out.items(), key=lambda kv: -abs(kv[1]))[:max_coins])
        kept = sum(abs(v) for v in keep.values())
        out = {c: v * gross / kept for c, v in keep.items()} if kept else keep
    if round_up:
        out = {c: (min_order if v > 0 else -min_order) if min_order / 2 <= abs(v) < min_order else v for c, v in out.items()}
    cap = max_gross if max_gross is not None else float("inf")
    total = sum(abs(v) for v in out.values())
    if total > cap > 0:
        out = {c: v * cap / total for c, v in out.items()}
    return out


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


def hold_within_band(targets: dict[str, float], current: dict[str, float], band: float) -> dict[str, float]:
    """Keep the current position of a coin when the new target is on the same side and within
    `band` (a share of the target) of it: traders that resize all day then cost no fees."""
    out = dict(targets)
    for coin, cur in current.items():
        t = targets.get(coin, 0.0)
        if t and cur and (t > 0) == (cur > 0) and abs(t - cur) < band * abs(t):
            out[coin] = cur
    return out
