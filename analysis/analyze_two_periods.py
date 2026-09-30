"""One-off: aggregate cycle_metrics + trades + skip_reasons across the two PolymarketData periods."""
import argparse, json, glob, os, collections

_parser = argparse.ArgumentParser(description=__doc__)
_parser.add_argument("--root", required=True, help="data root containing the 5.23-5-25/ and 5.25-5-29/ period directories")
ROOT = _parser.parse_args().root
PERIODS = {
    "OLD (5.23-5.25, T3 on)": os.path.join(ROOT, "5.23-5-25", "data", "telemetry"),
    "NEW (5.25-5.29, T3 off)": os.path.join(ROOT, "5.25-5-29", "data", "telemetry"),
}

def last_nonempty_json(path):
    last = None
    with open(path, "r", encoding="utf-8", errors="replace") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                last = json.loads(line)
            except Exception:
                continue
    return last

def iter_json(path):
    with open(path, "r", encoding="utf-8", errors="replace") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                yield json.loads(line)
            except Exception:
                continue

for label, tdir in PERIODS.items():
    print("=" * 80)
    print(label)
    print("=" * 80)
    cyc_files = sorted(glob.glob(os.path.join(tdir, "*.cycle_metrics.ndjson")))
    for cf in cyc_files:
        day = os.path.basename(cf).split(".")[0]
        # First and last row to get cumulative + per-day deltas
        first = None
        last = None
        n = 0
        skip_acc = collections.Counter()
        book_acc = collections.Counter()
        ws_conn_true = 0
        for d in iter_json(cf):
            if first is None:
                first = d
            last = d
            n += 1
            sr = d.get("skip_reason_counts") or {}
            # skip_reason_counts is cumulative-per-cycle snapshot? take last instead
            bs = d.get("book_stats") or {}
            if d.get("ws_connected"):
                ws_conn_true += 1
        if last is None:
            continue
        sr = last.get("skip_reason_counts") or {}
        bs = last.get("book_stats") or {}
        print(f"\n  [{day}] cycles={n}  ws_connected_ratio={ws_conn_true/n:.0%}")
        print(f"    PnL(last):  total={last.get('total_pnl')}  daily={last.get('daily_pnl')}  realized_daily={last.get('realized_daily_pnl')}  unrealized={last.get('unrealized_pnl')}")
        print(f"    positions:  open={last.get('open_positions')}  pos_value={last.get('current_position_value')}")
        print(f"    arbs_found_total={last.get('arbs_found_total')}  arbs_executed_total={last.get('arbs_executed_total')}  t0_total={last.get('t0_opportunities_total')}  directional_total={last.get('directional_signals_total')}")
        print(f"    today:      arbs_found={last.get('arbs_found_today')}  arbs_exec={last.get('arbs_executed_today')}  t0={last.get('t0_opportunities_today')}  directional={last.get('directional_signals_today')}")
        print(f"    live_succ={last.get('live_successes_total')}  sim_succ={last.get('simulated_successes_total')}  live_sub={last.get('live_submissions_total')}  sim_sub={last.get('simulated_submissions_total')}")
        print(f"    markets_scanned={last.get('markets_scanned')}  universe={last.get('universe_market_count')}  research_count={last.get('research_count')}")
        print(f"    skip_reason_counts(last): {dict(sorted(sr.items(), key=lambda x:-x[1])[:12]) if isinstance(sr,dict) else sr}")
        print(f"    book_stats(last): {bs}")
