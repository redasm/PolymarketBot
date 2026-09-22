"""T2 UPDOWN Phase 2 单测: spot feed / ref tracker / pricer / collector 分支."""

from __future__ import annotations

import time

import pytest

from polymarket_arb.spot_feed import UpdownRefTracker, parse_spot_pairs
from polymarket_arb.strategies.updown_pricer import (
    UpdownPricer,
    parse_updown_slug,
)


# --- parse_spot_pairs ---

def test_parse_spot_pairs_defaults_and_override():
    m = parse_spot_pairs(["btc", "eth", "xrp"], "btc:BTCUSD")
    assert m["btc"] == "BTCUSD"      # override wins
    assert m["eth"] == "ETHUSDT"     # built-in default
    assert m["xrp"] == "XRPUSDT"     # synthesized {SYM}USDT


def test_parse_spot_pairs_ignores_blank_and_malformed():
    m = parse_spot_pairs(["btc", ""], "garbage,eth:,:ETHUSDT, sol:SOLUSDT ")
    assert m == {"btc": "BTCUSDT", "sol": "SOLUSDT"} or "btc" in m


# --- BinanceSpotFeed sigma readiness (warmup sufficiency) ---

def test_spot_feed_default_warmup_makes_slow_sigma_ready():
    """Regression: warmup_klines must cover slow_minutes (360) so slow sigma is
    ready immediately. A too-small warmup (e.g. 120) left get_sigma_15m=None for
    the first ~6h, silently disabling all UPDOWN pricing."""
    from polymarket_arb.spot_feed import BinanceSpotFeed
    import time as _t
    feed = BinanceSpotFeed(pairs={"btc": "BTCUSDT"}, window_secs=[900],
                           fast_minutes=60, slow_minutes=360, min_bars=20)
    closes = [50000.0 * (1 + 0.0004 * ((i % 5) - 2)) for i in range(feed._warmup_klines)]
    feed._vol["btc"].warmup_from_closes(closes, int(_t.time() * 1000))
    sigma = feed.get_sigma_15m("btc")
    assert sigma is not None and sigma > 0



# --- slug parsing ---

@pytest.mark.parametrize("slug,sym,win,slot", [
    ("btc-updown-15m-1780272000", "btc", 900, 1780272000),
    ("eth-up-down-5m-1780272000", "eth", 300, 1780272000),
    ("sol-updown-60m-100", "sol", 3600, 100),
])
def test_parse_updown_slug_ok(slug, sym, win, slot):
    parsed = parse_updown_slug(slug)
    assert parsed is not None
    assert (parsed.symbol, parsed.window_sec, parsed.slot) == (sym, win, slot)


@pytest.mark.parametrize("slug", ["", "will-trump-win-2024", "btc-updown-15m", "random"])
def test_parse_updown_slug_rejects_non_updown(slug):
    assert parse_updown_slug(slug) is None


# --- UpdownRefTracker ---

def test_ref_tracker_locks_fresh_window_start():
    t = UpdownRefTracker()
    next_start = t.slot_for(t._start_ts, 900) + 900
    now = next_start + 3
    t.observe("btc", 900, 50000.0, now_sec=now)
    slot = t.slot_for(now, 900)
    assert t.get_ref("btc", 900, slot) == 50000.0


def test_ref_tracker_does_not_overwrite_within_window():
    t = UpdownRefTracker()
    next_start = t.slot_for(t._start_ts, 900) + 900
    now = next_start + 3
    t.observe("btc", 900, 50000.0, now_sec=now)
    t.observe("btc", 900, 51000.0, now_sec=now + 120)
    slot = t.slot_for(now, 900)
    assert t.get_ref("btc", 900, slot) == 50000.0  # first observation sticks


def test_ref_tracker_mid_join_window_returns_none():
    """A window whose start predates process launch is untrusted (we never saw
    the true start price), so get_ref must return None."""
    t = UpdownRefTracker()
    mid_slot = t.slot_for(t._start_ts - 1000, 900)
    t.observe("btc", 900, 50000.0, now_sec=t._start_ts + 1)  # observed mid-window
    assert t.get_ref("btc", 900, mid_slot) is None


def test_ref_tracker_slot_mismatch_returns_none():
    t = UpdownRefTracker()
    next_start = t.slot_for(t._start_ts, 900) + 900
    t.observe("btc", 900, 50000.0, now_sec=next_start + 3)
    assert t.get_ref("btc", 900, next_start + 900) is None  # wrong (future) slot


# --- UpdownPricer ---

class _FakeFeed:
    def __init__(self, spot=50250.0, sigma=0.004, ref=50000.0):
        self._spot, self._sigma, self._ref = spot, sigma, ref
        self.ref_tracker = self
    def get_spot(self, sym): return self._spot
    def get_sigma_15m(self, sym): return self._sigma
    def get_ref(self, sym, w, slot): return self._ref


def test_pricer_prices_up_when_spot_above_ref():
    slot = 1780272000
    p = UpdownPricer(_FakeFeed(spot=50250.0, ref=50000.0))
    fv = p.price(slug=f"btc-updown-15m-{slot}", now_sec=slot + 300)  # tau=600
    assert fv.ok
    assert fv.fair_up > 0.5  # spot above ref -> Up favored
    assert abs(fv.fair_up + fv.fair_down - 1.0) < 1e-6
    assert 0.55 <= fv.confidence <= 0.70
    assert fv.tau_sec == pytest.approx(600.0)


def test_pricer_skips_when_no_ref():
    class NoRef(_FakeFeed):
        def get_ref(self, sym, w, slot): return None
    fv = UpdownPricer(NoRef()).price(slug="btc-updown-15m-1780272000", now_sec=1780272300)
    assert not fv.ok and fv.reason == "updown_no_ref"


def test_pricer_skips_when_sigma_not_ready():
    class NoSig(_FakeFeed):
        def get_sigma_15m(self, sym): return None
    fv = UpdownPricer(NoSig()).price(slug="btc-updown-15m-1780272000", now_sec=1780272300)
    assert not fv.ok and fv.reason == "updown_sigma_not_ready"


def test_pricer_skips_when_no_spot():
    class NoSpot(_FakeFeed):
        def get_spot(self, sym): return None
    fv = UpdownPricer(NoSpot()).price(slug="btc-updown-15m-1780272000", now_sec=1780272300)
    assert not fv.ok and fv.reason == "updown_no_spot"


def test_pricer_skips_tau_too_small():
    slot = 1780272000
    p = UpdownPricer(_FakeFeed(), min_tau_sec=30.0)
    fv = p.price(slug=f"btc-updown-15m-{slot}", now_sec=slot + 890)  # tau=10
    assert not fv.ok and fv.reason == "updown_tau_too_small"


def test_pricer_rejects_unparseable_slug():
    fv = UpdownPricer(_FakeFeed()).price(slug="not-an-updown-market")
    assert not fv.ok and fv.reason == "updown_slug_unparsed"


# --- collector UPDOWN branch (integration) ---

from polymarket_arb.main_helpers.signal_collectors import collect_statistical_strategy_signals
from polymarket_arb.models import MarketInfo, OrderBookLevel, OrderBookSnapshot, TokenInfo
from polymarket_arb.strategies.statistical_model import StatisticalMispricingDetector
from tests.conftest import make_test_config


def _updown_market(cid="ud1", up_mid=0.50, down_mid=0.50, spread=0.01):
    return MarketInfo(
        condition_id=cid,
        question="Bitcoin Up or Down - May 31, 8:00PM-8:15PM ET",
        slug="btc-updown-15m-1780272000",
        tokens=[
            TokenInfo(token_id=f"{cid}-up", outcome="Up", price=up_mid),
            TokenInfo(token_id=f"{cid}-down", outcome="Down", price=down_mid),
        ],
        active=True,
        closed=False,
        event_slug="btc-updown-15m-1780272000",
    )


def _snap(token_id, mid, spread=0.01, depth=500.0):
    half = spread / 2.0
    return OrderBookSnapshot(
        token_id=token_id,
        best_bid=mid - half,
        best_ask=mid + half,
        bids=[OrderBookLevel(mid - half, depth)],
        asks=[OrderBookLevel(mid + half, depth)],
    )


class _Analyzer:
    def __init__(self, snaps):
        self._snaps = snaps
    def get_snapshot(self, tid):
        return self._snaps.get(tid)


class _StubPricer:
    """Returns a fixed fair value regardless of inputs (网络解耦)."""
    def __init__(self, fair_up=0.80, ok=True, reason="ok"):
        self._fair_up, self._ok, self._reason = fair_up, ok, reason
    def price(self, *, slug, event_slug="", now_sec=None):
        from polymarket_arb.strategies.updown_pricer import UpdownFairValue
        if not self._ok:
            return UpdownFairValue(ok=False, reason=self._reason)
        return UpdownFairValue(
            ok=True, reason="ok", fair_up=self._fair_up, fair_down=1 - self._fair_up,
            z_score=1.0, confidence=0.62, tau_sec=600.0, ref_px=50000.0,
            s_now=50250.0, sigma_15m=0.004, symbol="btc", window_sec=900, slot=1780272000,
        )


def _cfg():
    return make_test_config(
        t2_updown_enabled=True,
        t2_updown_spot_feed_enabled=True,
        t2_updown_max_spread_bps=500.0,
        t2_min_deviation=0.05,
        t2_min_top_depth=100.0,
    )


def test_collector_emits_updown_buy_up_signal():
    cfg = _cfg()
    market = _updown_market(up_mid=0.50, down_mid=0.50, spread=0.01)
    analyzer = _Analyzer({
        "ud1-up": _snap("ud1-up", 0.50),
        "ud1-down": _snap("ud1-down", 0.50),
    })
    detector = StatisticalMispricingDetector(min_deviation=cfg.t2_min_deviation, min_confidence=0.0)
    signals = collect_statistical_strategy_signals(
        config=cfg, candidate_markets=[market], ob_analyzer=analyzer,
        detector=detector, updown_pricer=_StubPricer(fair_up=0.80),
    )
    assert len(signals) == 1
    sig = signals[0]
    assert sig.signal_type == "buy_up"
    assert sig.payload["action"] == "BUY_UP"
    assert sig.payload["token_id"] == "ud1-up"          # explicit, correct side
    assert sig.payload["model_prob"] == pytest.approx(0.80)
    assert sig.payload["deviation"] == pytest.approx(0.30, abs=1e-6)
    assert sig.confidence == pytest.approx(0.62)


def test_collector_wide_spread_passes_updown_gate_but_blocks_generic():
    """UPDOWN spread门 (500bps) lets a 300bps book through; the same market
    would be blocked by the generic 120bps T2 gate."""
    cfg = _cfg()
    market = _updown_market(up_mid=0.50, down_mid=0.50, spread=0.015)  # 1.5c spread ~300bps
    analyzer = _Analyzer({
        "ud1-up": _snap("ud1-up", 0.50, spread=0.015),
        "ud1-down": _snap("ud1-down", 0.50, spread=0.015),
    })
    detector = StatisticalMispricingDetector(min_deviation=cfg.t2_min_deviation, min_confidence=0.0)
    signals = collect_statistical_strategy_signals(
        config=cfg, candidate_markets=[market], ob_analyzer=analyzer,
        detector=detector, updown_pricer=_StubPricer(fair_up=0.80),
    )
    assert len(signals) == 1  # passes UPDOWN's looser gate


def test_collector_blocks_updown_spread_over_dedicated_ceiling():
    cfg = make_test_config(
        t2_updown_enabled=True, t2_updown_spot_feed_enabled=True,
        t2_updown_max_spread_bps=200.0, t2_min_deviation=0.05, t2_min_top_depth=100.0,
    )
    market = _updown_market(spread=0.05)  # 5c spread ~1000bps > 200
    analyzer = _Analyzer({
        "ud1-up": _snap("ud1-up", 0.50, spread=0.05),
        "ud1-down": _snap("ud1-down", 0.50, spread=0.05),
    })
    detector = StatisticalMispricingDetector(min_deviation=cfg.t2_min_deviation, min_confidence=0.0)
    signals = collect_statistical_strategy_signals(
        config=cfg, candidate_markets=[market], ob_analyzer=analyzer,
        detector=detector, updown_pricer=_StubPricer(fair_up=0.80),
    )
    assert signals == []


def test_collector_skips_when_deviation_below_min():
    cfg = _cfg()
    market = _updown_market(up_mid=0.78, down_mid=0.22)
    analyzer = _Analyzer({
        "ud1-up": _snap("ud1-up", 0.78),
        "ud1-down": _snap("ud1-down", 0.22),
    })
    detector = StatisticalMispricingDetector(min_deviation=cfg.t2_min_deviation, min_confidence=0.0)
    # fair_up=0.80, up_mid=0.78 -> dev=0.02 < 0.05
    signals = collect_statistical_strategy_signals(
        config=cfg, candidate_markets=[market], ob_analyzer=analyzer,
        detector=detector, updown_pricer=_StubPricer(fair_up=0.80),
    )
    assert signals == []


def test_collector_ignores_updown_when_pricer_none():
    """Phase 1 behaviour: no pricer -> UPDOWN市场不产 updown 信号 (落入通用T2,
    其 up/down 非 yes/no, detector 仍可能跑但不会产 updown_* 类型)."""
    cfg = _cfg()
    market = _updown_market()
    analyzer = _Analyzer({"ud1-up": _snap("ud1-up", 0.50), "ud1-down": _snap("ud1-down", 0.50)})
    detector = StatisticalMispricingDetector(min_deviation=cfg.t2_min_deviation, min_confidence=0.0)
    signals = collect_statistical_strategy_signals(
        config=cfg, candidate_markets=[market], ob_analyzer=analyzer,
        detector=detector, updown_pricer=None,
    )
    assert all(not s.signal_type.startswith("buy_up") and not s.signal_type.startswith("buy_down") for s in signals)


