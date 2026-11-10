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
