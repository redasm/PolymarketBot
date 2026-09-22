[English](../en/research-findings.md) · [中文](../zh/research-findings.md)

# Empirical findings — what was tested and what failed

This is the most important document in the repository.

Between April and September 2026 every strategy tier in this bot was built,
instrumented, run in shadow mode or with small live capital, and then tested
against its own recorded data. **All of them were falsified.** None reached
positive expectancy after real fees, real latency, and honest accounting.

The code is published as research infrastructure and as a record of the
negative results — not as a profitable trading system. If you are looking for
something to run with money, this is not it. If you are looking for a worked
example of how retail prediction-market edges disappear under measurement, read
on.

## Summary

| Line | Verdict | Decisive evidence |
|---|---|---|
| T0 structural arbitrage | **Dead** | On 7 days of real tick data, every `Σask < 1` was a crossed book. Clean net-positive arbitrage count: 0. |
| T1 cross-platform | **Not pursued** | Requires a hand-maintained Polymarket↔Kalshi pairing table and capital on two venues; never reached a testable sample. |
| T2 statistical (general) | **Negative** | 14 of 15 shadow positions lost; fees were ~half the gross loss. |
| T2 UPDOWN 15-minute | **Look-ahead artefact** | +$443/week collapsed to 9% survival under 1s delay injection. The leak was entirely on the entry side. |
| T2 UPDOWN 5-minute | **Negative at real fees** | Shadow used `fee_rate=0.005`; the true rate for these markets is `0.07`, a 14× understatement. Corrected, it needs `min_deviation ≥ 0.08` to break even. |
| T3 market making | **Dead** | 53 of 53 closed positions had `close < open` (mean −0.0246). Break-even would require $10,949/day in liquidity rewards; actual rewards were $0. |
| Wallet alpha (copy trading) | **Dead** | −28.6% ROI followed to settlement; BUY_YES hit rate 29%; still negative with fees zeroed. |
| Weather (T2 sub-strategy) | **Negative** | Turned negative once forecasts were made lead-honest; Brier score worse than the market's. |
| TypeSafe Jev information layer | **Falsified on public sources** | Brier skill −1.49 vs the order book; the information that would beat the book is not in public news. |

Live trading has been stopped. The final live criterion, set in June 2026, was:
"if the bot still produces zero signals reaching the order layer after another
week, withdraw the capital." It did, and the capital was withdrawn.

---

## T0 — structural arbitrage

**Claim.** When `Σ ask < 1 - fees` across the outcomes of a market or event, you
can buy every side and lock in the difference.

**Why the first result looked good.** Scanning downsampled order book snapshots
produced 4,667 apparently net-positive opportunities. That number is an
artefact: snapshots taken at intervals and then joined across tokens splice
together quotes that never coexisted. Any such reconstruction manufactures
arbitrage.

**What the tick stream says.** Re-running the same detection over seven days of
continuous per-token tick data (`data/ticks/*.ndjson`, recorded with
`TICK_RECORD_ENABLED=true`):

- Every remaining `Σ ask < 1` instance was a **crossed book** — the venue's feed
  transiently published `bid > ask` on at least one leg. A crossed book
  trivially satisfies the inequality and is not tradable.
- After excluding crossed books, the count of clean net-positive structural
  arbitrage over the full window was **zero**.
- The earlier "high edge" signals (5–13%) were all in this category. Genuine
  edge, where it existed at all, was 1–3% and confined to multi-outcome events
  — and it did not survive depth verification.

This is also a real bug in the detector: it did not guard against crossed
books. The lesson generalises — **any arbitrage detector must reject crossed
books before evaluating the inequality**, or it will report the venue's feed
glitches as free money.

**Reproduce**: `analysis/t0_tick_scan.py`, `analysis/t0_window_survival.py`,
`analysis/build_t0_dataset.py`, `analysis/run_t0_backtest.py`.

**Related bug, fixed**: T0 opportunities that passed verification were then
blocked by the T2 daily-loss budget. BINARY and MULTI_OUTCOME now bypass the
daily-loss check; DIRECTIONAL signals remain subject to it. This mattered for
correctness but did not change the verdict — there were no opportunities to
pass through.

---

## T2 — statistical arbitrage

### General statistical signals

Shadow data from 23–30 May 2026: **14 of 15** T2 positions lost money. Fees
accounted for roughly half the gross loss. The model was not finding
mispricings; it was paying the spread to express noise.

### The fee model was wrong by 14×

Polymarket moved to Fee V2 on 30 March 2026:

```
taker_fee = rate · p · (1 - p)      maximised at p = 0.5
```

The shadow evaluation used the default `rate = 0.005`. For crypto Up/Down
markets the actual `feeSchedule.rate` is **0.07** — fourteen times higher. Every
shadow PnL number computed before this was found is unusable.

The production code path was already correct: both T0 and T2 resolve the rate
`for_market` rather than using the default, so live sizing never understated
fees. Only the offline evaluation was wrong. **Verify with
`scripts/verify_polymarket_fees.py` before trusting any backtest number.**

Corrected results: 5-minute UPDOWN requires `min_deviation ≥ 0.08` to turn
positive. 15-minute UPDOWN stays negative at every threshold.

### UPDOWN 15-minute: a textbook look-ahead artefact

The shadow run showed **+$443/week**. It was not real.

Delay-injection test — re-run the identical strategy with entries delayed by 1
second, changing nothing else:

- **9% of the profit survived.**
- The leak was entirely on the entry side: 1 second after the recorded entry
  signal, the ask had already moved **+3.67 cents** against us.

In other words the "signal" was reading a price the strategy could not have
transacted at. A latency gate is now a permanent fixture of the evaluation
harness — no shadow result is reportable without it.

**Reproduce**: `analysis/updown_threshold_sweep.py`,
`analysis/updown_realism.py`, `analysis/updown_bias_check.py`,
`analysis/verify_updown_markets.py`.

Note that crypto Up/Down markets moved to 5-minute windows during 2026, and the
tight `ARB_MARKET_FOCUS_KEYWORDS` used in the $10 canary configuration did not
match them at all — so the canary was structurally unable to see the only
markets under active study.

---

## T3 — market making, killed by adverse selection

53 closed maker positions. **All 53** closed below their open price; mean
`close - open = -0.0246`. Not "mostly negative" — unanimously negative.

That distribution is the signature of adverse selection: the resting order only
gets filled when the informed side wants the other side of it. A retail market
maker without a toxicity gate and sub-second cancel capability is providing free
optionality to faster participants.

The reward math does not rescue it either. Covering the observed loss rate from
Polymarket liquidity rewards would require **$10,949/day** in rewards on this
book. Actual rewards received: **$0**.

Anti-sniping protections were subsequently implemented (jump pause, stability
confirmation, quote filtering, post-fill cooldown, chase limits). **They made
per-share economics worse**, not better — the protections that avoid toxic fills
also avoid the benign ones, and the surviving fills were no better.

**Preconditions before anyone reopens T3**: a working toxicity gate and
fast-cancel path that demonstrably reduce the adverse-selection rate, measured
on the same 53-position accounting. Without those, this is a known-losing
configuration.

**Reproduce**: `analysis/t3_maker_replay.py`.

---

## Wallet alpha — copy trading

Three independent lines of evidence, all pointing the same way:

1. **Followed to settlement: −28.6% ROI.** Not marked-to-market, not
   markout-at-horizon — actual settlement outcomes.
2. **BUY_YES hit rate 29%.** Substantially below chance for the population of
   markets involved, i.e. the signal was reliably *inverted*, not merely noisy.
3. **Still negative with fees set to zero.** The loss is not a cost-structure
   problem that better execution could fix.

Publicly profitable addresses are not a tradable signal at the latency and
information level available to a follower. This strategy line is abandoned. The
backtest engine for it is kept because it is reusable, not because the strategy
is.

**Reproduce**: `analysis/run_wallet_alpha_backtest.py`,
`analysis/wallet_alpha_settle_validate.py`. Rebuild the datasets first — see
[../../analysis/README.md](../../analysis/README.md).

---

## Weather

The weather sub-strategy prices Polymarket temperature contracts off Open-Meteo
GFS ensemble forecasts.

It looked positive until the forecasts were made **lead-honest** — that is,
until each decision used only the forecast vintage that was actually published
before the decision time, rather than the current best forecast for that date.
After that correction it turned negative, and its Brier score was **worse than
the market's own implied probabilities**.

The same measurement also surveyed which open weather APIs are usable for this;
those availability notes are in
[references.md](references.md#weather-data).

**Reproduce**: `analysis/weather_lead_forecasts.py`,
`analysis/weather_upper_bound.py`, `analysis/run_weather_open_data_test.py`.

---

## TypeSafe Jev — can a calibrated scoring model beat the book?

TypeSafe's Jev is a "System One" model: it does not generate text, it answers
typed questions about a `state` and returns calibrated probabilities. Every
Polymarket market is literally a yes/no question, so this looked like an
unusually good fit.

### Historical replay (19 September 2026, 93 settled markets, 141 snapshots)

| Metric | Jev | Order book | Verdict |
|---|---|---|---|
| Brier (same 107-row two-sided subset) | 0.2686 | **0.1080** | skill **−1.49** |
| AUC | 0.589 | **0.936** | almost no discrimination |
| Skill by category | politics −0.50 / sports −1.85 / crypto −4.29 / other −2.59 | — | **no category positive** |
| Skill by time to resolution | <2d −0.29 / 2–14d −0.52 / 14–60d −2.09 / >60d −4.51 | — | worse further out, never positive |

Two systematic biases:

1. **Probabilities collapse toward zero.** 133 of 141 rows landed in
   `p_yes ∈ [0, 0.2)`; that bucket's predicted mean was 0.073 against a realised
   YES rate of 0.241 (the book's mean for the same bucket was 0.337).
2. **No cross-market normalisation.** Summing mutually exclusive candidates
   should give ≈1. Observed: Colombia round 1 Σ=0.44 (book 1.008), Busan mayor
   Σ=0.52 (1.002), NBA champion Σ=0.21 (1.005), NBA coach of the year Σ=0.08
   (1.000). And in the other direction, LA mayor Σ=0.58 across 7 candidates
   where the book summed to 0.013. Per-question independence is by design, but
   it means you must normalise yourself on multi-outcome events or the
   probabilities are not comparable.

The edge simulation *looked* positive. It was two traps stacked:

- **Pseudo-replication** — three variants of the same market share one
  settlement outcome, so betting per row counts one bet three times. Only the
  `dedup=market` rows are meaningful.
- **Long-tail single bets** — at `dedup=market, θ=0.10`: 40 bets, 25% hit rate,
  **median −0.0151 per unit, only 10 of 40 positive**. The +1.77 total drops to
  +0.90 if you remove the single largest winner, and the as-of variant drops to
  **−0.105**.

A contamination probe (does the model just remember the outcome?) came back
negative: adding an `as_of` hint barely changed answers (MAE 0.0245, 2 of 141
crossing 0.5), while discrimination stayed low. So this is a genuine negative
result, not a false negative caused by leakage. The inverse also holds — if a
*forward* shadow ever shows performance far above the book, suspect
contamination first.

Two positive by-products: the `jev_ambiguous` score was well distributed (mean
0.330, max 0.770) and its top-scoring market really was ambiguously worded, so
using it as a **resolution-wording ambiguity alarm** is still promising. And
engineering-wise it was clean: 287 calls, 0 errors, 0 rate limits, $0.0079
total.

### Information-layer audit (the follow-up question)

The replay fed the model no information — just question text, resolution
criteria, and days to resolution. So it falsified "can Jev's prior beat the
book", not "can Jev plus collected information beat the book". The prerequisite
for the second question is not the model, it is whether the information exists
publicly before settlement.

`analysis/probe_news_coverage.py` queried GDELT DOC 2.0 for English news in
`[as_of − 7d, as_of]` for those 93 settled markets (143 minutes of runtime,
thanks to free-tier rate limits).

**The information layer is not the bottleneck. The scissor gap is.**

| Group | Markets | Book Brier | Book direction accuracy | Jev Brier |
|---|---|---|---|---|
| Well covered (≥8 articles) | 28 | **0.0325** | **96%** | 0.2617 |
| Sparse (1–7) | 9 | 0.0113 | 100% | 0.4706 |
| No coverage (0) | 34 | 0.0806 | 89% | 0.2820 |

Where news exists, the book has already priced it — 25 well-covered markets
produced exactly one where the book was badly wrong. And on that one:

> *Will Abelardo de la Espriella win the 1st round of the 2026 Colombian
> presidential election?* — book 0.2415 one day before settlement, actual
> outcome YES, 24 articles in window. Jev said 0.06 (0.03 with the as-of hint)
> at confidence 0.87–0.93. **The book was wrong and the model was confidently
> more wrong.**

Coverage by category shows the gap directly:

| Category | Markets | Hits | True zero | No query generated | Rate limited | Median articles when hit |
|---|---|---|---|---|---|---|
| politics | 43 | 24 | 8 | 0 | 11 | **22** |
| sports | 11 | 7 | 0 | 0 | 4 | **21** |
| crypto | 30 | 3 | 3 | **20** | 4 | 10 |
| other | 8 | 2 | 3 | 0 | 3 | 13 |
| macro | 1 | 1 | 0 | 0 | 0 | 17 |

Politics and sports are well covered — and that is exactly where the book is
most accurate. Small crypto events ("will X launch a token by date Y"), where
the book is thinnest and most likely to be wrong, had no retrievable news at all
in 26 of 30 cases.

**Conclusion**: making this work needs neither a better model nor a better
prompt. It needs information the book has not read yet — official data releases
at source, on-chain events, on-the-ground reporting. That is a news-speed
business, and on public sources this path is falsified.

---

## Transferable methodology

The negative results above are specific to this bot. The methods that produced
them are not, and they are the part of this repository worth reusing.

### Delay injection is the fastest look-ahead test

Re-run the strategy with entries delayed by a realistic latency (1s is a good
first probe) and change nothing else. If most of the profit disappears, the
strategy was reading prices it could not have traded at. This single test
invalidated the largest apparent edge in this project's history in one
afternoon.

Look-ahead bias has at least four distinct layers, and they need separate tests:

1. **Data timestamp leakage** — using a bar/tick whose timestamp postdates the
   decision.
2. **Entry-price leakage** — assuming a fill at a price observed at signal time,
   with no latency. *This is where the 15-minute UPDOWN edge came from.*
3. **Parameter leakage** — thresholds tuned on the same data used to score them.
4. **Universe leakage** — selecting the instrument set using information from
   after the decision (e.g. "markets that later had volume").

### An in-sample R² that looks good is a leak, not a discovery

For order-flow-imbalance style regressions on this kind of data, an in-sample
R² above 0.2 should be read as evidence of leakage, not of signal. Check the
feature construction before celebrating.

### Verify the fee rate per market, never the default

Polymarket Fee V2 is `rate · p · (1-p)`, and `rate` varies by market. Crypto
Up/Down markets carry `feeSchedule.rate = 0.07` against a common default of
0.005. Using the default silently understated costs by 14× and made a losing
strategy look profitable. Resolve fees `for_market`, and re-derive them with
`scripts/verify_polymarket_fees.py` whenever a result depends on them.

### Article counts are not information

When measuring whether news coverage exists, hit counts answer "was there
news?", not "was there *relevant* news". Several markets in the audit hit the
25-article cap with content that had nothing to do with the resolution — a
person-name query pulled in an entire city's local news. Always read a manual
sample.

Three measurement bugs found while running that audit, each of which would have
inverted the conclusion on its own:

| Bug | Consequence | Fix |
|---|---|---|
| "No query could be generated" counted as "zero coverage" | First pass reported 34 true zeros (48%); 20 of them had never actually been queried | Split status into `ok` / `rate_limited` / `no_query` — real numbers were 14 / 20 / 22 |
| GDELT `enddatetime` is not precise | 70 articles across 13 markets had `seendate` past the as-of cutoff | Hard second filter on `seendate`, with the excess counted separately |
| HTTP 429 indistinguishable from "zero hits" | 2 of 5 markets were false zeros at a 1s request interval | 7s interval plus backoff, 429 recorded as its own error code |
| Query degenerating to one generic word | `"Bitcoin"` retrieved tribal council news and exchange download pages — **feeding noise is worse than feeding nothing**, because downstream treats it as evidence | Discard single-word queries; require ≥2 terms |

### $10 cannot validate a strategy

Distinguishing a genuinely positive expectancy from noise needs on the order of
500–1000 independent trades. A $10 book supports tens of fills, and at that size
fixed costs (gas plus fees, $0.01–0.05 per trade) are 30–100% of the expected
per-trade spread capture. A $2 loss on $10 tells you nothing about the strategy.

What $10 *can* validate: that the bot submits, cancels, reconnects, and handles
errors correctly. Use it for engineering validation only, and do not mistake an
engineering test budget for a strategy validation budget.

### Snapshots are not tick data

Any analysis that joins across instruments using periodically sampled snapshots
will manufacture opportunities that never existed simultaneously. The 4,667
phantom T0 opportunities came from exactly this. If the claim is about
simultaneity, the data must be a continuous per-token stream.

---

## What remains open

Several ideas shipped behind disabled flags because validating them needed data
that free-tier APIs would not provide. Those are tracked separately, with
graduation and rejection criteria for each, in
[pending-validations.md](pending-validations.md). None of them are known to
work; they are simply untested rather than falsified.

The one positive lead from the TypeSafe work — using the ambiguity score as a
resolution-wording alarm rather than as a probability source — was never
pursued.
