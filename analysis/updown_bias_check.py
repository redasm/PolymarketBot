"""
UPDOWN look-ahead bias analysis
Joins strategy_signals (has updown context) with positions_lifecycle (has pnl/hold_sec)
"""
import argparse, json, os, statistics

_parser = argparse.ArgumentParser(description=__doc__)
_parser.add_argument("--telemetry-dir", required=True, help="directory with <date>.strategy_signals.ndjson and <date>.positions_lifecycle.ndjson")
_parser.add_argument("--dates", nargs="+", default=["2026-06-02", "2026-06-03", "2026-06-04", "2026-06-05"])
_args = _parser.parse_args()
TELE = _args.telemetry_dir
DATES = _args.dates

# ── 1. Load strategy_signals with updown context ─────────────────────────────
signals = {}  # signal_id -> record
for d in DATES:
    path = os.path.join(TELE, f"{d}.strategy_signals.ndjson")
    if not os.path.exists(path):
        continue
    with open(path, encoding="utf-8") as f:
        for line in f:
            if "buy_up" not in line and "buy_down" not in line:
                continue
            try:
                obj = json.loads(line)
            except:
                continue
            sig_type = obj.get("signal_type", "")
            if "buy_up" not in sig_type and "buy_down" not in sig_type:
                continue
            sid = obj.get("signal_id", "")
            if sid:
                signals[sid] = obj

print(f"Loaded {len(signals)} UPDOWN signals from strategy_signals")

# ── 2. Load positions_lifecycle closed positions ──────────────────────────────
positions = []  # list of closed position rows
for d in DATES:
    path = os.path.join(TELE, f"{d}.positions_lifecycle.ndjson")
    if not os.path.exists(path):
        continue
    with open(path, encoding="utf-8") as f:
        for line in f:
            if "buy_up" not in line and "buy_down" not in line:
                continue
            try:
                obj = json.loads(line)
            except:
                continue
            if obj.get("event") != "position_closed":
                continue
            dc = obj.get("decision_context") or {}
            sig_type = dc.get("signal_type", "")
            if "buy_up" not in sig_type and "buy_down" not in sig_type:
                continue
            positions.append(obj)

print(f"Loaded {len(positions)} closed UPDOWN positions from lifecycle")

# ── 3. Join on signal_id ──────────────────────────────────────────────────────
rows = []
unmatched = 0
for pos in positions:
    dc = pos.get("decision_context") or {}
    sid = dc.get("signal_id", "")
    sig = signals.get(sid)
    ud = {}
    if sig:
        payload = sig.get("payload") or {}
        ud = payload.get("updown") or {}

    entry_px  = pos.get("open_price")
    exit_px   = pos.get("close_price")
    pnl       = pos.get("realized_pnl")
    hold_sec  = pos.get("hold_sec")
    sig_type  = dc.get("signal_type", "")
    entry_ts  = pos.get("open_ts")   # unix epoch
    exit_ts   = pos.get("close_ts")
    market    = pos.get("market_id", "")[:24]
    title     = (dc.get("event_title") or "")[:40]

    rows.append({
        "sig":        sig_type,
        "entry_px":   entry_px,
        "exit_px":    exit_px,
        "pnl":        pnl,
        "hold_sec":   hold_sec,
        "ref_px":     ud.get("ref_px"),
        "s_now":      ud.get("s_now"),
        "sigma_15m":  ud.get("sigma_15m"),
        "tau_sec":    ud.get("tau_sec"),
        "z_score":    ud.get("z_score"),
        "fair_up":    ud.get("fair_up"),
        "fair_down":  ud.get("fair_down"),
        "market_prob":ud.get("market_prob") or dc.get("best_ask_at_decision"),
        "deviation":  (sig.get("payload") or {}).get("deviation") if sig else None,
        "window_sec": ud.get("window_sec"),
        "slot":       ud.get("slot"),
        "entry_ts":   entry_ts,
        "title":      title,
        "signal_id":  sid,
        "matched":    bool(sig and ud),
    })
    if not (sig and ud):
        unmatched += 1

print(f"Matched with full updown context: {len(rows)-unmatched}/{len(rows)}")

# ── A. Detail table ───────────────────────────────────────────────────────────
print("\n=== A. UPDOWN 仓位明细 ===")
def f4(v): return f"{v:.4f}" if v is not None else "N/A"
def fi(v): return f"{int(v)}" if v is not None else "N/A"
def fp(v): return f"{v:.1f}%" if v is not None else "N/A"

hdr = f"{'#':>3} {'sig':<12} {'entry':>7} {'exit':>7} {'pnl':>8} {'hold_s':>7} {'tau_s':>6} {'win%':>6} {'ref_px':>9} {'s_now':>9} {'dev':>7} {'z':>6}  title"
print(hdr); print("-"*len(hdr))
for i, r in enumerate(rows, 1):
    tau = r["tau_sec"]
    win = r["window_sec"] or 900
    win_pct = (win - tau) / win * 100 if tau is not None else None
    print(f"{i:>3} {r['sig']:<12} {f4(r['entry_px']):>7} {f4(r['exit_px']):>7} "
          f"{f4(r['pnl']):>8} {fi(r['hold_sec']):>7} {fi(tau):>6} {fp(win_pct):>6} "
          f"{f4(r['ref_px']):>9} {f4(r['s_now']):>9} {f4(r['deviation']):>7} {f4(r['z_score']):>6}  {r['title']}")

# ── B. tau_sec distribution ───────────────────────────────────────────────────
tau_vals = sorted(r["tau_sec"] for r in rows if r["tau_sec"] is not None)
n = len(tau_vals)
print(f"\n=== B. tau_sec 分布 (n={n}) ===")
if tau_vals:
    def pct(p): return tau_vals[min(int(p/100*n), n-1)]
    print(f"min={tau_vals[0]:.0f}  p25={pct(25):.0f}  median={pct(50):.0f}  p75={pct(75):.0f}  max={tau_vals[-1]:.0f}")
    win_positions = [(900 - t) / 900 * 100 for t in tau_vals]
    ps = sorted(win_positions)
    print(f"\n窗口内位置% 分布:")
    print(f"  min={ps[0]:.1f}%  p25={ps[int(n*0.25)]:.1f}%  median={ps[int(n*0.5)]:.1f}%  p75={ps[int(n*0.75)]:.1f}%  max={ps[-1]:.1f}%")
    buckets = {"<20%": 0, "20-50%": 0, "50-80%": 0, "80-100%": 0, ">100%(tau<0)": 0}
    for p in win_positions:
        if p < 0:    buckets[">100%(tau<0)"] += 1
        elif p < 20: buckets["<20%"] += 1
        elif p < 50: buckets["20-50%"] += 1
        elif p < 80: buckets["50-80%"] += 1
        else:        buckets["80-100%"] += 1
    for k, v in buckets.items():
        bar = "█" * v
        print(f"  {k:<16}: {v:>3} ({v/n*100:>4.0f}%)  {bar}")

# ── C. Direction hit rate & pnl vs deviation ─────────────────────────────────
print(f"\n=== C. 方向命中率 ===")
up_rows = [r for r in rows if "buy_up" in r["sig"] and r["entry_px"] and r["exit_px"]]
dn_rows = [r for r in rows if "buy_down" in r["sig"] and r["entry_px"] and r["exit_px"]]
if up_rows:
    hit = sum(1 for r in up_rows if r["exit_px"] > r["entry_px"])
    print(f"buy_up   价格上涨: {hit}/{len(up_rows)} ({hit/len(up_rows)*100:.0f}%)")
if dn_rows:
    hit = sum(1 for r in dn_rows if r["exit_px"] < r["entry_px"])
    print(f"buy_down 价格下跌: {hit}/{len(dn_rows)} ({hit/len(dn_rows)*100:.0f}%)")

pnl_all = [r["pnl"] for r in rows if r["pnl"] is not None]
profit  = [r for r in rows if r["pnl"] is not None and r["pnl"] > 0]
loss    = [r for r in rows if r["pnl"] is not None and r["pnl"] <= 0]
print(f"\n盈利: {len(profit)} 笔  亏损: {len(loss)} 笔  总PnL: {sum(pnl_all):.4f} USDC")
print(f"平均PnL: {sum(pnl_all)/len(pnl_all):.4f}" if pnl_all else "")

dev_p = [r["deviation"] for r in profit if r["deviation"] is not None]
dev_l = [r["deviation"] for r in loss   if r["deviation"] is not None]
if dev_p: print(f"盈利组 deviation 均值: {sum(dev_p)/len(dev_p):.4f}  median: {sorted(dev_p)[len(dev_p)//2]:.4f}")
if dev_l: print(f"亏损组 deviation 均值: {sum(dev_l)/len(dev_l):.4f}  median: {sorted(dev_l)[len(dev_l)//2]:.4f}")

# ── D. hold_sec vs tau_sec ────────────────────────────────────────────────────
print(f"\n=== D. hold_sec vs tau_sec ===")
both = [(r["hold_sec"], r["tau_sec"], r["pnl"]) for r in rows if r["hold_sec"] is not None and r["tau_sec"] is not None]
settled    = [(h,t,p) for h,t,p in both if t > 0 and abs(h - t) < 60]
early_exit = [(h,t,p) for h,t,p in both if t > 0 and h < t * 0.5]
print(f"有效样本: {len(both)}")
print(f"  持仓到结算 |hold-tau|<60s : {len(settled)} 笔  avg_pnl={sum(p for _,_,p in settled)/len(settled):.4f}" if settled else "  持仓到结算: 0 笔")
print(f"  提前止损   hold < tau/2   : {len(early_exit)} 笔  avg_pnl={sum(p for _,_,p in early_exit)/len(early_exit):.4f}" if early_exit else "  提前止损: 0 笔")

# ── E. ref_px vs s_now (look-ahead bias core check) ──────────────────────────
print(f"\n=== E. ref_px vs s_now (look-ahead bias 核心检验) ===")
has_both = [r for r in rows if r["ref_px"] is not None and r["s_now"] is not None]
same = [r for r in has_both if abs(r["ref_px"] - r["s_now"]) < 0.5]   # <0.5 crypto price unit
diff = [r for r in has_both if abs(r["ref_px"] - r["s_now"]) >= 0.5]
print(f"有 ref_px+s_now 数据: {len(has_both)}/{len(rows)}")
if has_both:
    print(f"  ref_px ≈ s_now (diff<0.5): {len(same)} ({len(same)/len(has_both)*100:.0f}%)  ← 若高占比则 look-ahead bias")
    print(f"  ref_px ≠ s_now (diff≥0.5): {len(diff)} ({len(diff)/len(has_both)*100:.0f}%)  ← 正常（窗口已走了一段）")
    diffs = [abs(r["ref_px"] - r["s_now"]) for r in has_both]
    print(f"  |ref_px - s_now| 统计: min={min(diffs):.2f}  median={sorted(diffs)[len(diffs)//2]:.2f}  max={max(diffs):.2f}")
    if diff:
        print("\n  样例 ref_px≠s_now（正常）:")
        for r in diff[:4]:
            print(f"    tau={fi(r['tau_sec'])}s  ref={f4(r['ref_px'])}  s_now={f4(r['s_now'])}  diff={abs(r['ref_px']-r['s_now']):.2f}  pnl={f4(r['pnl'])}")

# ── F. tau_sec < 0 ────────────────────────────────────────────────────────────
neg_tau = [r for r in rows if r["tau_sec"] is not None and r["tau_sec"] < 0]
print(f"\n=== F. tau_sec < 0 (窗口已结算后才入场 — 严重 look-ahead) ===")
print(f"共 {len(neg_tau)} 笔")
for r in neg_tau[:5]:
    import datetime
    ts_str = datetime.datetime.utcfromtimestamp(r["entry_ts"]).strftime("%Y-%m-%dT%H:%M:%S") if r["entry_ts"] else "?"
    print(f"  tau={fi(r['tau_sec'])}s  sig={r['sig']}  pnl={f4(r['pnl'])}  ref_px={f4(r['ref_px'])}  s_now={f4(r['s_now'])}  entry={ts_str}")

# ── G. Delay-injection test proxy ─────────────────────────────────────────────
print(f"\n=== G. Delay-injection 代理检验 ===")
print("检验逻辑：若信号在窗口末尾（tau_sec<120s）入场，且随后盈利，说明模型知道了快结算时的走势 → look-ahead")
late_entries = [r for r in rows if r["tau_sec"] is not None and r["tau_sec"] < 120]
early_entries = [r for r in rows if r["tau_sec"] is not None and r["tau_sec"] >= 120]
def avg_pnl(lst): return sum(r["pnl"] for r in lst if r["pnl"] is not None) / max(1, sum(1 for r in lst if r["pnl"] is not None))
def win_rate(lst):
    valid = [r for r in lst if r["pnl"] is not None]
    return sum(1 for r in valid if r["pnl"] > 0) / len(valid) if valid else 0
print(f"  tau<120s  (窗口末尾入场): {len(late_entries)} 笔  avg_pnl={avg_pnl(late_entries):.4f}  胜率={win_rate(late_entries)*100:.0f}%")
print(f"  tau≥120s  (窗口早期入场): {len(early_entries)} 笔  avg_pnl={avg_pnl(early_entries):.4f}  胜率={win_rate(early_entries)*100:.0f}%")
print("  → 若末尾入场胜率显著高于早期入场，是 look-ahead bias 的信号")
