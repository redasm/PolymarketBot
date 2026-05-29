"""C0 gate: 验证 Polymarket 平台是否上架短 horizon crypto UPDOWN 市场.

背景: 修复计划工作块 C 要为 T2 引入短 horizon、高流动性的 crypto UPDOWN 标的
(fair_value_model 唯一有 edge 的地方)。但 bot 当前扫描 universe (经 focus
keyword + hot pool 过滤) 里 0 个这类市场。本脚本直接查 gamma API 全量活跃市场,
回答 C0: 平台上到底有没有这类市场? 若有, 流动性如何 (能否过 T2 质量门)?

这是只读探测, 不下任何单, 无需钱包私钥参与交易 (但需 .env 提供 gamma_host)。
在能连 gamma 的部署服务器上运行:

    python -m analysis.verify_updown_markets            # 默认查 7 天内
    python -m analysis.verify_updown_markets --days 3   # 只看 3 天内
    python -m analysis.verify_updown_markets --limit 1500 --json

判定标准 (对照 .env 默认):
  - T2_MAX_SPREAD_BPS=120, T2_MIN_TOP_DEPTH=100 —— 流动性下限
  - 短 horizon: 默认 < 7 天 (UPDOWN 通常是当日/数小时)
"""

from __future__ import annotations

import argparse
import json
import re
import time
from datetime import datetime, timezone

import requests

from polymarket_arb.config import ArbConfig
from polymarket_arb.market_scanner import MarketScanner
from polymarket_arb.main_helpers.signal_collectors import _market_horizon_days


def _gamma_get(host: str, path: str, params: dict) -> list | dict | None:
    """Direct read-only GET against gamma. Returns parsed JSON or None on failure."""
    url = f"{host.rstrip('/')}/{path.lstrip('/')}"
    try:
        resp = requests.get(url, params=params, timeout=15)
        resp.raise_for_status()
        return resp.json()
    except Exception as e:  # noqa: BLE001 - probe script, surface any failure as None
        print(f"  [gamma 请求失败] {url} params={params}: {e}")
        return None


def probe_by_enddate(host: str, days: float, page_limit: int = 500) -> list[dict]:
    """Probe B: 按 endDate 升序拉取最快到期的市场 (不受 volume 排序偏差).

    这是关键修正: `MarketScanner.fetch_active_markets` 按 volume_24hr 降序拉,
    短命的 15m UPDOWN 市场单个 volume 极小, 必然排在分页末尾被漏掉。按
    endDate 升序则把最快到期的市场顶到最前, 无视 volume。
    """
    now = datetime.now(timezone.utc)
    out: list[dict] = []
    offset = 0
    while offset < 3000:  # 安全上限
        rows = _gamma_get(host, "/markets", {
            "active": "true", "closed": "false",
            "limit": min(page_limit, 100), "offset": offset,
            "order": "endDate", "ascending": "true",
        })
        if not isinstance(rows, list) or not rows:
            break
        stop = False
        for raw in rows:
            end = raw.get("endDate") or raw.get("end_date_iso") or ""
            hd = None
            try:
                ed = datetime.fromisoformat(str(end).replace("Z", "+00:00"))
                hd = (ed - now).total_seconds() / 86400.0
            except Exception:
                pass
            if hd is not None and hd > days:
                stop = True  # 已超过窗口, 升序后面只会更远
                break
            q = raw.get("question") or ""
            slug = raw.get("slug") or ""
            if not (_CRYPTO_PAT.search(q) or _CRYPTO_PAT.search(slug) or "updown" in slug.lower()):
                continue
            out.append({
                "question": q, "slug": slug,
                "horizon_days": round(hd, 4) if hd is not None else None,
                "is_updown_like": bool(_UPDOWN_PAT.search(q)) or "updown" in slug.lower(),
                "liquidity": round(float(raw.get("liquidity") or 0.0), 2),
                "volume_24h": round(float(raw.get("volume24hr") or raw.get("volume_24hr") or 0.0), 2),
                "end_date": end,
            })
        if stop:
            break
        offset += len(rows)
        time.sleep(0.15)
    return out


def probe_by_slug(host: str, slots_ahead: int = 8) -> list[dict]:
    """Probe A: 按 mlmodelpoly 的 slug 约定直查 btc-updown-15m-{slot}.

    slot = floor(unix/900)*900 (UTC 15分钟边界对齐)。查当前 + 未来 slots_ahead
    个窗口。命中即证明这类市场存在 (无视它在 volume 排序里的位置)。
    """
    now = int(time.time())
    cur_slot = (now // 900) * 900
    patterns = ["btc-updown-15m-{}", "eth-updown-15m-{}"]
    out: list[dict] = []
    for i in range(slots_ahead + 1):
        slot = cur_slot + i * 900
        for pat in patterns:
            slug = pat.format(slot)
            events = _gamma_get(host, "/events", {"slug": slug})
            if isinstance(events, list) and events:
                mkts = events[0].get("markets") or [{}]
                m = mkts[0]
                out.append({
                    "slug": slug,
                    "question": m.get("question") or events[0].get("title") or "",
                    "liquidity": round(float(m.get("liquidity") or 0.0), 2),
                    "volume_24h": round(float(m.get("volume24hr") or 0.0), 2),
                    "outcomes": m.get("outcomes"),
                    "end_date": m.get("endDate") or "",
                })
    return out

# UPDOWN / 现货方向类市场的文本特征 (宽松匹配, 宁可多召回让人工核对)
_UPDOWN_PAT = re.compile(
    r"(up or down|higher than|go up|go down|above|below|dip to|reach |hit \$|"
    r"close (above|below|higher|lower)|\bat \d|\d(am|pm)\b| ET\b|today|tonight|hourly)",
    re.I,
)
_CRYPTO_PAT = re.compile(r"\b(bitcoin|btc|ethereum|eth|crypto|solana|\bsol\b|xrp|dogecoin|doge)\b", re.I)


def main() -> int:
    ap = argparse.ArgumentParser(description="C0 gate: probe gamma for short-horizon crypto UPDOWN markets")
    ap.add_argument("--days", type=float, default=7.0, help="horizon upper bound in days (default 7)")
    ap.add_argument("--limit", type=int, default=1500, help="max markets to fetch from gamma")
    ap.add_argument("--dotenv-path", default=".env")
    ap.add_argument("--json", action="store_true", help="emit JSON instead of table")
    args = ap.parse_args()

    config = ArbConfig.from_env(dotenv_path=args.dotenv_path)
    scanner = MarketScanner(config)
    markets = scanner.fetch_active_markets(limit=args.limit)

    crypto_short: list[dict] = []
    crypto_all = 0
    for m in markets:
        is_crypto = bool(_CRYPTO_PAT.search(m.question or ""))
        if is_crypto:
            crypto_all += 1
        hd = _market_horizon_days(m)
        if hd is None or hd < 0 or hd > args.days:
            continue
        if not is_crypto:
            continue
        crypto_short.append({
            "question": m.question,
            "horizon_days": round(hd, 3),
            "is_updown_like": bool(_UPDOWN_PAT.search(m.question or "")),
            "liquidity": round(float(m.liquidity), 2),
            "volume_24h": round(float(m.volume_24h), 2),
            "condition_id": m.condition_id,
            "end_date": m.end_date,
        })

    crypto_short.sort(key=lambda r: r["horizon_days"])
    # 流动性达标 = 有一定 liquidity (粗筛, 真正 spread/depth 要订阅 orderbook 才知道)
    liquid = [r for r in crypto_short if r["liquidity"] >= 100.0]
    updown_like = [r for r in crypto_short if r["is_updown_like"]]

    if args.json:
        print(json.dumps({
            "fetched": len(markets),
            "crypto_total": crypto_all,
            "crypto_short_horizon": len(crypto_short),
            "crypto_short_updown_like": len(updown_like),
            "crypto_short_liquid_ge100": len(liquid),
            "markets": crypto_short,
        }, ensure_ascii=False, indent=1))
        return 0

    print(f"\n=== C0 gate: 短 horizon (<{args.days}d) crypto 市场探测 ===")
    print(f"gamma 拉取活跃市场: {len(markets)}")
    print(f"crypto 相关 (任意 horizon): {crypto_all}")
    print(f"crypto 且短 horizon (<{args.days}d): {len(crypto_short)}")
    print(f"  其中 UPDOWN 文本特征: {len(updown_like)}")
    print(f"  其中 liquidity>=100: {len(liquid)}")
    print("\n  horizon  updown  liq      vol24h    question")
    for r in crypto_short[:40]:
        print(f"  {r['horizon_days']:6.2f}d  {'Y' if r['is_updown_like'] else '-':^6} {r['liquidity']:8.0f} {r['volume_24h']:9.0f}  {r['question'][:60]}")

    # --- Probe B: endDate 升序 (不受 volume 排序偏差, 关键修正) ---
    print(f"\n=== Probe B: 按 endDate 升序拉取最快到期的 crypto 市场 (<{args.days}d) ===")
    by_end = probe_by_enddate(config.gamma_host, args.days)
    by_end.sort(key=lambda r: (r["horizon_days"] if r["horizon_days"] is not None else 1e9))
    by_end_liquid = [r for r in by_end if r["liquidity"] >= 100.0]
    by_end_updown = [r for r in by_end if r["is_updown_like"]]
    print(f"命中 crypto 短 horizon: {len(by_end)}  (其中 UPDOWN 特征 {len(by_end_updown)}, liquidity>=100 {len(by_end_liquid)})")
    print("\n  horizon  updown  liq      vol24h    slug / question")
    for r in by_end[:40]:
        hd = f"{r['horizon_days']:6.3f}d" if r["horizon_days"] is not None else "   ?  "
        label = (r["slug"] or r["question"])[:55]
        print(f"  {hd}  {'Y' if r['is_updown_like'] else '-':^6} {r['liquidity']:8.0f} {r['volume_24h']:9.0f}  {label}")

    # --- Probe A: slug 直查 (复刻 mlmodelpoly, 决定性证据) ---
    print("\n=== Probe A: slug 直查 btc/eth-updown-15m-{slot} (当前+未来2小时) ===")
    by_slug = probe_by_slug(config.gamma_host, slots_ahead=8)
    print(f"命中: {len(by_slug)}")
    for r in by_slug[:20]:
        print(f"  liq={r['liquidity']:8.0f} vol24h={r['volume_24h']:9.0f} outcomes={r['outcomes']}  {r['slug']}")

    print("\n=== C0 综合判定 (以 Probe A/B 为准, 基线 volume 探测有偏差) ===")
    found_any = bool(crypto_short or by_end or by_slug)
    liquid_any = bool(liquid or by_end_liquid or [r for r in by_slug if r["liquidity"] >= 100.0])
    updown_any = bool(updown_like or by_end_updown or by_slug)
    if by_slug:
        print(f"  [GO] slug 直查命中 {len(by_slug)} 个 updown-15m 市场 -> 平台确有这类标的;")
        print("       基线 volume 探测漏掉它们, 正印证 Phase 1 boost 的必要性。")
        print("       下一步: 开 T2_UPDOWN_ENABLED=true 跑 bot, 看订阅后真实 spread/depth 能否过质量门。")
    elif by_end_updown:
        print(f"  [GO] endDate 升序命中 {len(by_end_updown)} 个短 horizon UPDOWN 特征市场 -> 值得进 bot 验证流动性。")
    elif by_end:
        print(f"  [WEAK] endDate 升序有 {len(by_end)} 个短 horizon crypto 市场但无明显 UPDOWN 特征 -> 人工核对 slug。")
    elif not found_any:
        print("  [STOP] 三条探测路径 (volume / endDate / slug) 均未命中 -> 平台确无短 horizon crypto UPDOWN 市场, C 暂停。")
    else:
        print("  [WEAK] 仅基线 volume 探测有少量命中 -> 数据不足, 人工核对。")
    print("  注: liquidity 是 gamma 粗口径; 真正 spread/top_depth 需订阅 orderbook 验证 (Phase 2)。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
