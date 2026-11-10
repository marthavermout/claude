"""Self-improvement: review the results, name the mistakes, make at most ONE change.

Guardrails against "improving" yourself into overfitting:
- judge a trader only after enough hours and closed trades (small samples are noise)
- one change per review, then a cooldown so the effect can actually be measured
- risk settings may only be made *stricter* automatically, never looser
- a dropped trader is blacklisted, so the bot can't flip-flop between the same names
- every decision and its reason is written to JOURNAL.md
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Callable

from .config import BotConfig, load_overrides, save_overrides
from .review import Review, build_review, to_markdown


@dataclass
class TuneRules:
    target_leaders: int = 3
    min_hours_to_judge: float = 72
    min_closed_trades: int = 5
    drop_leader_below: float = -0.08  # shadow return
    copy_gap_warn: float = 0.10  # we lag the trader's own result by >10 points
    min_trades_for_cost_rules: int = 20
    max_fee_drag: float = 0.5  # fees > 50% of realized pnl
    max_churn_ratio: float = 0.25
    min_trade_usd_step: float = 1.5
    min_trade_usd_cap: float = 100.0
    max_halt_days_per_week: int = 2
    leverage_step: float = 0.75
    leverage_floor: float = 1.0
    cooldown_hours: float = 48
    inactive_hours: float = 48  # replace a trader that gave nothing to copy for this long


@dataclass
class Decision:
    mistakes: list[str] = field(default_factory=list)
    observations: list[str] = field(default_factory=list)
    change: str | None = None
    skipped_reason: str | None = None


JOURNAL_HEADER = "# Copybot logboek\n\nNieuwste review bovenaan.\n\n"

Selector = Callable[[set[str], int], list[str]]  # (exclude, n) -> addresses


def decide(review: Review, week: Review, cfg: BotConfig, ov: dict, rules: TuneRules,
           selector: Selector, now: datetime) -> Decision:
    d = Decision()
    leaders = list(ov.get("leaders", cfg.leaders))
    blacklist = set(ov.get("blacklist", []))
    risk = ov.setdefault("risk", {})

    # ---- diagnose ----
    weak = None
    for s in review.leaders:
        if s.hours >= rules.inactive_hours and s.trades == 0 and weak is None:
            weak = s
            d.mistakes.append(f"Trader `{s.address[:10]}…` heeft in {s.hours:.0f} uur niets gedaan dat we konden kopiëren.")
            continue
        if s.hours < rules.min_hours_to_judge or s.closed_trades < rules.min_closed_trades:
            d.observations.append(f"`{s.address[:10]}…`: nog te weinig data om te beoordelen "
                                  f"({s.hours:.0f}u, {s.closed_trades} gesloten trades)")
            continue
        if s.shadow_return < rules.drop_leader_below and weak is None:
            weak = s
            d.mistakes.append(f"Trader `{s.address[:10]}…` kopiëren leverde {s.shadow_return:+.2%} op "
                              f"(grens {rules.drop_leader_below:+.0%}).")
        if s.leader_return - s.shadow_return > rules.copy_gap_warn:
            d.observations.append(f"`{s.address[:10]}…` zelf {s.leader_return:+.2%}, onze kopie {s.shadow_return:+.2%}: "
                                  "we lopen achter (te snel, of stortingen/opnames vertekenen zijn saldo).")

    costly = False
    if review.trades >= rules.min_trades_for_cost_rules:
        if review.realized and review.fee_drag > rules.max_fee_drag:
            costly = True
            d.mistakes.append(f"Kosten slokken {review.fee_drag:.0%} van het resultaat op.")
        if review.churn_ratio > rules.max_churn_ratio:
            costly = True
            d.mistakes.append(f"{review.churn_ratio:.0%} van de trades draait binnen een uur de vorige om (churn).")

    halts = len(week.halt_days)
    too_many_halts = halts >= rules.max_halt_days_per_week
    if too_many_halts:
        d.mistakes.append(f"{halts} dagen met dagverlies-stop in de afgelopen week.")

    if review.btc_return is not None and review.hours >= rules.min_hours_to_judge and review.total_return < review.btc_return:
        d.observations.append(f"Slechter dan gewoon BTC vasthouden ({review.total_return:+.2%} vs {review.btc_return:+.2%}).")

    # ---- fill empty seats (always allowed, not a 'change' to an experiment) ----
    if len(leaders) < rules.target_leaders and weak is None:
        new = selector(blacklist | set(leaders), rules.target_leaders - len(leaders))
        if new:
            ov["leaders"] = leaders + new
            d.change = "Traders aangevuld: " + ", ".join(f"`{a[:10]}…`" for a in new)
            return d

    # ---- at most one change, with cooldown ----
    last_change = ov.get("last_change")
    if last_change and now - datetime.fromisoformat(last_change) < timedelta(hours=rules.cooldown_hours):
        if d.mistakes:
            d.skipped_reason = (f"Afkoelperiode: vorige wijziging was minder dan {rules.cooldown_hours:.0f}u geleden; "
                                "eerst het effect daarvan meten.")
        return d

    if weak is not None:
        replacement = selector(blacklist | set(leaders), 1)
        leaders.remove(weak.address)
        ov["leaders"] = leaders + replacement
        ov["blacklist"] = sorted(blacklist | {weak.address})
        repl = f"`{replacement[0][:10]}…`" if replacement else "niemand (geen kandidaat gevonden)"
        d.change = f"Trader `{weak.address[:10]}…` vervangen door {repl}."
    elif too_many_halts and cfg.risk.max_leverage > rules.leverage_floor:
        new_lev = max(rules.leverage_floor, round(cfg.risk.max_leverage * rules.leverage_step, 2))
        risk["max_leverage"] = new_lev
        d.change = f"Max hefboom verlaagd: {cfg.risk.max_leverage}x → {new_lev}x."
    elif costly and cfg.risk.min_trade_usd < rules.min_trade_usd_cap:
        new_min = min(rules.min_trade_usd_cap, round(cfg.risk.min_trade_usd * rules.min_trade_usd_step, 2))
        risk["min_trade_usd"] = new_min
        d.change = f"Minimale trade-grootte verhoogd: ${cfg.risk.min_trade_usd} → ${new_min} (minder kleine aanpassingen)."
    return d


def tune(cfg: BotConfig, selector: Selector, rules: TuneRules | None = None,
         now: datetime | None = None, if_due_hours: float | None = None) -> Decision | None:
    rules = rules or TuneRules()
    now = now or datetime.now(timezone.utc)
    data_dir = Path(cfg.data_dir)
    ov = load_overrides(data_dir)
    seats_open = len(ov.get("leaders", cfg.leaders)) < rules.target_leaders
    if if_due_hours is not None and ov.get("last_review") and not seats_open:
        if now - datetime.fromisoformat(ov["last_review"]) < timedelta(hours=if_due_hours):
            return None

    overall = build_review(data_dir, active_leaders=cfg.leaders)
    day = build_review(data_dir, active_leaders=cfg.leaders, since_days=1)
    week = build_review(data_dir, since_days=7)
    d = decide(overall, week, cfg, ov, rules, selector, now)

    n = ov.get("reviews", 0) + 1
    ov["reviews"] = n
    ov["last_review"] = now.isoformat()
    log = ov.setdefault("review_log", [])
    log.append({"time": now.isoformat(), "n": n, "mistakes": d.mistakes, "observations": d.observations,
                "decision": d.change or d.skipped_reason or "niets veranderen"})
    del log[:-60]
    if d.change:
        ov["last_change"] = now.isoformat()
        ov.setdefault("history", []).append({"time": now.isoformat(), "change": d.change})
    save_overrides(data_dir, ov)

    entry = [f"# Review #{n} — {now:%Y-%m-%d %H:%M} UTC", ""]
    entry.append("**Gevonden fouten:**")
    entry += [f"- {m}" for m in d.mistakes] or ["- geen"]
    if d.observations:
        entry += ["", "**Observaties (geen automatische actie):**"] + [f"- {o}" for o in d.observations]
    entry += ["", f"**Besluit:** {d.change or d.skipped_reason or 'niets veranderen'}", ""]
    entry.append(to_markdown(day, "Laatste 24 uur"))
    entry.append(to_markdown(overall, "Sinds de start"))
    text = "\n".join(entry) + "\n---\n\n"

    (data_dir / "REPORT.md").write_text(text)
    journal = data_dir / "JOURNAL.md"
    previous = journal.read_text() if journal.exists() else ""
    if previous.startswith(JOURNAL_HEADER):
        previous = previous[len(JOURNAL_HEADER):]
    journal.write_text(JOURNAL_HEADER + text + previous)
    return d
