"""Pre-live gates for shadow strategies.

These are *blocking* validation checks meant to run before any shadow/dry-run
strategy is graduated to live trading. Unlike the replay runner (which simulates
a strategy from scratch), a gate audits already-recorded shadow fills for known
failure modes that make a "profitable" shadow result un-realizable in practice.

Current gates:
- ``latency_gate`` — delay-injection look-ahead detector. Re-prices every shadow
  position at ``t + delay`` using the recorded tick stream and reports how much
  net PnL survives. A strategy whose edge evaporates under sub-second latency is
  riding look-ahead bias, not alpha.
"""
