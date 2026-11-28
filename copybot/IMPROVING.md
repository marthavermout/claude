# Daily improvement routine

This is the checklist a scheduled Claude session follows once a day. Goal: propose **one**
code or settings change that would have produced a better result, prove it on data the idea
was not based on, and open a pull request only if it passes. A human merges; never merge yourself.

## 1. Get the data
```bash
git fetch origin copybot-data 3.x
git worktree add /tmp/botdata origin/copybot-data     # bot memory: logs, journal, replay/
cd copybot
python -m copybot -c config.ci.toml -d /tmp/botdata review
python -m copybot -c config.ci.toml -d /tmp/botdata review --days 1
```
Also read `/tmp/botdata/JOURNAL.md` (newest first), `trades.csv`, `leaders.csv`, `overrides.json`,
and the open/closed PRs titled `copybot:` from earlier days (don't re-propose a rejected idea
unless there is new evidence).

If `replay/` holds less than **72 hours** of data: do not propose strategy changes (anything that
could be overfit). You may still propose a bug fix or a reliability fix with a unit test. If there
is nothing like that, stop and report "not enough data yet" with the current numbers.

## 1b. Compare the bots
Eighteen paper bots run side by side (nine strategies, each with $1000 and with $50) on the same market (`basis` = `config.ci.toml`, the others in
`bots/*.toml`, data in `/tmp/botdata/bots/<name>/`, summary in `/tmp/botdata/bots.json`).
Run `review` for each and compare return, max drawdown, fees and number of trades.
- `patronen` (Pia) does not copy anyone: it trades chart patterns (`copybot/patterns.py`) with a trend
  filter. Its 30-day backtest before going live was a loss (482 trades, -$148 without and about -$47 with
  the trend filter). Judge it like the others; `copybot.patterns.backtest` replays candles to test changes.
- `trend` (Tom) and `funding` (Fien) read the market themselves (`copybot/quant.py`, settings fixed before
  testing). 208-day backtest on 15 coins, first 70% / last 30% (exam), costs and funding included:
  Tom +11.1% / -0.6% (max drawdown 17% / 11%, BTC -1.5% / -7.0%); Fien -9.8% / -13.3% (funding earned, but
  unhedged price moves and costs were larger). `quant.simulate` replays hourly data to test changes.
- Since 11 Nov 2026 every bot pays/receives hourly funding like a real account (before that it was ignored).
- Review of all bots on 12 Nov 2026 (user request). Applied, each tested on first 70% / last 30%:
  Tom slower trend 48h/14d (+11.1%/-0.6% -> +18.1%/+5.4%, lower drawdown); Fien only with the trend
  (-9.8%/-13.3% -> -1.3%/-0.5%, still no profit); Pia needs take-profit >= 1.5x stop-loss (52 days of
  15m candles: -$255/-$87 -> -$153/-$27, still a loss in every variant: no proven edge). Rejected: Pia on
  1h candles (sign flips between periods), 20% rebalance band for copy bots, Fien with a higher threshold.
  Copy bots were NOT changed: only ~2 days of data.
- Every strategy also runs as a `<name>-mini` bot with $50 (`bots/*-mini.toml`, added 12 Nov 2026 at the
  user's request; they replace the single $100 `klein` bot). Copy minis mirror their big bot's traders
  (`follow_bot`) and never pick their own. A replay showed $50 makes 2-4 trades where $1000 makes 30-90,
  because of the $10 minimum order. The final verdict on starting with a small real amount must use the
  minis' results, not the $1000 bots.
- Since 12 Nov 2026 all bots run in one process (`copybot run-all`, `copybot/fleet.py`) on a shared, cached
  client: each trader is fetched once per round for every bot, rounds are every 5 s (was 30-60 s per bot),
  and a websocket on the 10 most active followed traders wakes the bots ~0.4 s after they trade (10 is
  Hyperliquid's per-connection limit). Budget measured live: ~310 requests/min. History rows (equity,
  leaders, replay) are written at most every 30 s; trades are always written.
- The "fees eat X% of the result" check now compares fees with the whole result (open positions
  included). Before, it only used closed trades and flagged Alex at 219% while he was up 5%.
- Pia (14 Nov 2026, user request "recognise all wave forms"): she now recognises 19 patterns
  (`patterns.ALL_PATTERNS`, incl. symmetrical triangle, rectangle, cup with handle, Elliott wave 3,
  engulfing candles) but only trades those in `patterns = [...]` in her config. Each pattern was tested
  alone; rule fixed beforehand: profit after costs on the first 70% AND last 30% of 52 days of 15m
  candles AND of 208 days of 1h candles, 10+ trades in each part. Only the symmetrical triangle passed
  (19 + 5 tests, so one pass could still be luck). Patterns she may not trade are logged in
  `pattern_seen.csv`. Weekly (Sundays): re-run the per-pattern test on fresh candles and check the
  seen patterns' outcomes; add a pattern only if it passes the same rule on data it was not picked on,
  remove the triangle if it stops passing. Note: the 1.5x reward/risk filter makes double/triple
  tops and bottoms and rectangles impossible to trade (their target equals their risk); retested
  without the filter on 14 Nov, all five still lost money.
- Fien minimum holding time (28 Nov 2026), rejected: she often opens on high funding and closes an hour
  later (one hour of funding ~0.005% vs ~0.13% round-trip cost). Holding at least 8h / 24h was tested with
  `quant.simulate` on 208 days, 15 coins: exam part -0.60% -> -0.70% / -2.20% (split 0.7) and
  -0.53% -> -1.38% / -4.34% (split 0.5). Worse in both, so not applied. Her live result is about -1%.
- Rebalance band (21 Nov 2026): `rebalance_band = 0.25` keeps a copied position while the target stays within
  25% of it. Bea made 671 HYPE trades in 14h as traders resized all day. On the exam part (splits 0.7 and 0.5)
  it won for Bea, Alex and Axel (+1.1 to +5.6 points) and is on there; Vera lost slightly and Bas (the
  reference bot) gained little, so they stay off. In-sample it cost ~1 point everywhere: re-check after a week.
- Pia weekly retest (23 Nov 2026, fresh candles: 52 days 15m, 208 days 1h): the symmetrical triangle failed
  the 15m exam part (-7.1% over 60 trades), so it is removed and Pia trades nothing (`patterns = ["none"]`)
  while still logging what she sees. The descending triangle passed on 1h (+2.9%/+16.1%) but had too few
  15m trades (8): not added. Re-test every Sunday; a pattern returns only if it passes both again.
- Minimum trade size vs account (20 Nov 2026): the tuner's churn fix raised Bas-mini's minimum trade to
  $22.50 on a $50 account; replaying basis' history showed it then makes 0 trades. The minimum is now capped
  at 20% of the starting balance (never below the exchange's $10), and a too-high value is repaired at once.
- Small accounts (16 Nov 2026): `max_coins` and `round_up_min` let a mini concentrate on its largest
  copy targets or round $5-10 targets up to the $10 minimum. Tested per mini on the big bots' recorded
  history (splits 0.7 and 0.5): only round-up for `alles-mini` passed, so only that is on. Vera's mini
  cannot trade at all ($50 x 15% per coin = $7.50 < $10). The amount matters far more than any trick:
  replaying Bas with $50/$100/$300/$500/$1000 gave +3.3/+7.2/+10.9/+11.5/+11.7%.
- `random` (Rik) is the **control group**: traders picked at random, not on past results. It answers
  "skill or luck?". A bot only counts as better when it beats **both Rik and holding BTC**.
- Only call a winner after **7+ days** and **30+ closed trades** per bot, and when the gap is larger
  than the day-to-day swings. Otherwise say "too early" with the current ranking.
- Luck test: take each bot's daily return minus Rik's daily return (from `equity.csv`, one value per
  UTC day). Report the mean and t = mean / (stdev / sqrt(days)). Only t > 2 over 14+ days counts as
  evidence; anything less is "not proven yet", whatever the ranking says.
- If a variant clearly beats `basis` and its settings are *not* riskier, propose making them the
  `basis` settings (one PR, with the comparison as evidence).
- If the winner is a riskier variant (e.g. `agressief`), do not promote it: tell the user the result
  and the extra risk (its drawdown) and let them decide.
- A variant that is clearly worst after 14 days may be replaced by a new idea (one per PR),
  e.g. different trader selection or a different `join_within_pct`.

## 2. Find the biggest mistake
Look for the one thing costing the most money or risk, e.g.:
- fees or churn eating the profit (many small or back-and-forth trades)
- one trader or one coin causing most of the loss
- copying too late (our copy lags the trader's own result)
- risk limits hit often, or never doing anything useful
- worse than simply holding BTC
- a bug: errors in the workflow logs, wrong numbers, crashes

Write down the hypothesis in one sentence before touching code.

## 3. Test it honestly
Baseline = the code on `3.x`. Candidate = your change. Use the **same** replay data for both.
```bash
# baseline (from a checkout of 3.x)
python -m copybot -c config.ci.toml -d /tmp/botdata backtest > /tmp/base.json
# candidate (from your branch); settings-only ideas can use --set on the baseline instead
python -m copybot -c config.ci.toml -d /tmp/botdata backtest > /tmp/cand.json
python -m unittest discover -s tests
```
The first 70% of history is **in-sample** (look there for ideas), the last 30% is
**out-of-sample** (the exam). Accept the change only if, out-of-sample:
- `total_return` is better than baseline, and
- `max_drawdown` is not worse by more than 1 percentage point, and
- all unit tests pass (add a test for the new behaviour).

Also check that the idea still holds with `--split 0.5`. If a result flips sign between splits,
it's noise: reject it.

Rules:
- One change per proposal. Small diff. No new dependencies.
- Never loosen risk limits (`max_leverage`, `max_position_pct`, loss limits) to get a better number.
- Never turn on real-money trading.
- Don't tune a number to many decimals. Round values (e.g. 15 → 30) or it's curve fitting.

## 4. Report
If it passed: commit, push and open a PR against `3.x` titled `copybot: <change>` containing
the hypothesis, the evidence (baseline vs candidate table for both splits, hours of data used),
the risks, and how to undo it.

If nothing passed: no PR. Report what you tried and why it failed. A rejected idea is a valid result.

## 5. Autonomous mode (user's standing instruction, 10 Nov 2026)
The user does not want to manage this by hand during the 7-week test (until 29 Dec 2026). So:
- **Fix mistakes every day, not only bugs**: churn, high fees, a trader with nothing to copy, wrong or
  missing data, a crashed or skipped run, a bot stuck at 0 trades. Small, tested fixes.
- **Strategy changes** (which settings are "basis") follow the rules in 1b and 3: evidence first.
- A change that passes all tests and rules may be **merged by you** (PR into `3.x`, then merge), so
  the user doesn't have to. After merging, restart the Copybot workflow right after a data save.
- Never: real money, loosening `basis` risk limits, deleting data, touching anything outside `copybot/`
  and its workflows.
- The user reads the daily report as a push notification on their phone: Dutch, short, plain words.
