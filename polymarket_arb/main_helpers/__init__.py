"""Helpers extracted from `polymarket_arb.main_loop`.

`main_loop.py` accreted ~3 100 lines / ~50 top-level functions across the
project's lifetime. To keep its public entry point (`main()`) reviewable
without breaking the call graph, pure / side-effect-free helpers are being
moved here in small, individually-tested phases.

Each sub-module owns one concern (scan focus, dashboard serialisation,
strategy-signal collection, …) so the orchestration code in `main_loop`
can shrink to just the wiring + run-loop.
"""
