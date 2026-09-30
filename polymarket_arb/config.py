"""配置管理：从环境变量读取所有参数，提供类型安全的访问."""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from pathlib import Path
from zoneinfo import ZoneInfo

from dotenv import load_dotenv

LOG = logging.getLogger(__name__)

_NO_WALLET_PLACEHOLDER = "research-mode"


def _env(key: str, default: str = "") -> str:
    return os.environ.get(key, default).strip()


def _env_float(key: str, default: float = 0.0) -> float:
    raw = _env(key)
    if not raw:
        return default
    try:
        return float(raw)
    except ValueError:
        LOG.warning("env %s=%r 无法解析为 float，使用默认值 %s", key, raw, default)
        return default


def _env_int(key: str, default: int = 0) -> int:
    raw = _env(key)
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError:
        LOG.warning("env %s=%r 无法解析为 int，使用默认值 %s", key, raw, default)
        return default


def _env_bool(key: str, default: bool = False) -> bool:
    raw = _env(key).lower()
    if not raw:
        return default
    return raw in ("true", "1", "yes", "on")


@dataclass(frozen=True)
class ArbConfig:
    """套利机器人完整配置，所有字段从环境变量读取."""

    # 钱包与认证
    private_key: str
    funder_address: str
    signature_type: int
    chain_id: int

    # 端点
    clob_host: str
    gamma_host: str
    clob_client_version: str
    clob_api_key: str
    clob_api_secret: str
    clob_api_passphrase: str

    # 套利参数
    min_edge_usd: float
    min_edge_pct: float
    max_order_size_usdc: float
    default_order_size_usdc: float
    scan_interval_sec: float
    # P0-dirty: minimum number of markets that must become "dirty"
    # (WS best bid/ask changed) before the main loop's inter-cycle
    # sleep is broken early. 1 = wake on any change (lowest latency,
    # higher CPU); higher values batch wakes for less-busy operators.
    dirty_market_wake_threshold: int
    market_fetch_limit: int
    market_universe_refresh_sec: float
    hot_market_pool_size: int
    hot_event_pool_size: int
    market_focus_keywords: str
    dry_run: bool
    live_trading_ack: bool
    live_require_portfolio_sync: bool
    live_allow_zero_taker_fee: bool
    live_max_order_size_usdc: float
    live_max_total_exposure_usdc: float
    live_min_net_edge_bps: float
    live_min_net_edge_usd: float
    live_max_orderbook_snapshot_age_sec: float
    live_min_ws_hit_ratio: float
    maker_strategy_enabled: bool
    min_liquidity: float
    min_volume_24h: float
    orderbook_snapshot_ttl_sec: float
    orderbook_ws_snapshot_max_age_sec: float
    orderbook_retry_count: int
    orderbook_retry_delay_sec: float
    orderbook_missing_cooldown_sec: float
    orderbook_batch_concurrency: int
    cross_platform_pairs_json: str
    polymarket_taker_fee_rate: float
    kalshi_taker_fee_rate: float
    max_multi_outcome_legs: int
    t0_min_multi_outcome_median_leg_price: float
    # 多结果套利完整性校验 (防漏腿伪套利)。检测器把 active 腿子集当成完整互斥集
    # 用 1.0-Σcost 算套利;若某条 active 腿 (典型是 volume≈0 的兜底腿) 没进 universe,
    # 漏腿会让"买全部结果<1"在数学上不成立。开启后要求实际腿数 == 事件声明的
    # active 腿总数 (来自 EventInfo.raw),不等则丢弃。
    t0_require_complete_partition: bool

    # 风险管理
    max_open_positions: int
    max_exposure_per_market: float
    max_total_exposure: float
    max_daily_loss: float
    max_consecutive_failures: int
    risk_event_cooldown_sec: float
    risk_pending_reservation_ttl_sec: float
    risk_halt_auto_recover_sec: float
    maker_stale_order_ttl_sec: float

    # 账户同步 / Data API
    portfolio_sync_enabled: bool
    portfolio_sync_interval_sec: float
    portfolio_sync_timeout_sec: float
    portfolio_sync_max_consecutive_failures: int
    data_api_host: str
    portfolio_sync_user_address: str

    # 飞书应用机器人通知
    feishu_app_id: str
    feishu_app_secret: str
    feishu_open_id: str
    feishu_api_base: str
    notification_cooldown_sec: float
    notify_on_arb_found: bool
    # In dry-run/shadow mode every detected opportunity would push a chat
    # notification but never produce a real trade — that drowns operators
    # in noise. This flag (default False) keeps `notify_on_arb_found`
    # itself usable while silencing only the shadow stream.
    notify_arb_found_in_shadow: bool
    notify_on_trade_success: bool
    notify_on_trade_failure: bool
    notify_on_fatal_error: bool
    notify_on_pnl_alert: bool
    notify_on_daily_summary: bool
    pnl_profit_alert_usdc: float
    pnl_loss_alert_usdc: float
    fatal_error_cooldown_sec: float
    daily_summary_time_hhmm: str
    daily_summary_timezone: str
    notification_state_file: str

    # 波动率
    vol_fast_minutes: int
    vol_slow_minutes: int
    vol_min_bars: int

    # Edge 引擎
    edge_min_bps: float
    edge_max_spread_bps: float
    edge_min_confidence: float
    edge_confidence_full_bps: float
    edge_confidence_imbalance_weight: float
    edge_volatility_spike_ratio: float
    edge_volatility_spike_penalty: float
    edge_volatility_calm_ratio: float
    edge_volatility_calm_boost: float
    t2_min_deviation: float
    t2_max_spread_bps: float
    t2_min_top_depth: float
    t2_max_complement_error_bps: float
    t2_max_signals_per_market_per_hour: int
    # T2 exit policy
    t2_stop_loss_bps: float
    t2_take_profit_capture_pct: float
    t2_max_hold_sec: float
    t2_exit_eval_interval_sec: float
    t2_optimal_stopping_enabled: bool
    # Number of equal tranches to scale out a T2 position over for the
    # "happy-path" exit triggers (take_profit / optimal_stopping). Each
    # trigger sells `size_remaining / remaining_tranches`, so over N
    # triggers the position is fully closed. stop_loss / time_stop /
    # floor_dump always exit the full remaining size regardless of this
    # setting. 1 = legacy single-stop behaviour. Kobylanski 2009: d-stop
    # strictly dominates 1-stop when the model is uncertain, because each
    # tranche locks in different price realisations of the same exit
    # decision rule.
    t2_scale_out_tranches: int
    # Dynamic (ATR-equivalent) stop-loss. When enabled, the exit
    # manager tracks a rolling per-position volatility (stdev of log
    # returns of recent mid prices) and sets `stop_bps = clamp(k *
    # vol_bps, min, max)`. Disabled by default — the static
    # T2_STOP_LOSS_BPS is the safe baseline. Direction comes from
    # HyperLiquid backtest article but the multiplier needs
    # calibration on Polymarket binary-contract dynamics, which differ
    # from perpetual futures. Until warm (fewer than
    # T2_STOP_LOSS_DYNAMIC_WARMUP price observations), the static stop
    # is used as a safe fallback.
    t2_stop_loss_dynamic_enabled: bool
    t2_stop_loss_dynamic_k: float
    t2_stop_loss_min_bps: float
    t2_stop_loss_max_bps: float
    t2_stop_loss_dynamic_warmup: int
    # T2 long-horizon guard: directional bets on markets resolving more than
    # `t2_long_horizon_days` away (or with no end_date at all) require a
    # higher net edge to enter. Long-horizon binary contracts are priced
    # more by risk premium than by short-term mean reversion, so the
    # default `live_min_net_edge_bps=25` is way too generous for them.
    t2_long_horizon_days: float
    t2_long_horizon_min_net_edge_bps: float
    # Hard horizon cap for T2 collector. Pre-fix this was a hard-coded
    # 90d in `collect_statistical_strategy_signals`, which dropped
    # ~50% of the universe on long-dated election / geopolitical
    # markets. 0 disables the cap.
    t2_max_horizon_days: float
    # Top-K per-cycle cap on T2 signals before the orchestrator's tier
    # budget step. Without it the collector emits one signal per
    # candidate market and the orchestrator skips 80+/cycle with
    # `tier_budget_below_min_order`. 0 = no cap.
    t2_max_signals_per_cycle: int
    # T2 UPDOWN substrate (Phase 1): when enabled, short-horizon spot-anchored
    # UP/DOWN markets (e.g. btc-updown-15m) get a priority boost in scan-pool
    # and WS-subscription selection so they aren't squeezed out by high-volume
    # long-horizon markets. Default off — gated until shadow confirms these
    # markets pass the T2 quality gate (spread/depth).
    t2_updown_enabled: bool
    t2_updown_priority_boost: float
    # UPDOWN markets carry ~0 24h volume per rotating window, so the
    # volume-sorted `fetch_active_markets` drops them before they ever reach
    # the scan pool. When `t2_updown_enabled`, the universe refresh issues a
    # dedicated slug-direct probe (`/events?slug={sym}-updown-{w}m-{slot}`) for
    # the current + next N windows, bypassing the volume filter. These params
    # control which assets / window lengths / look-ahead the probe covers.
    t2_updown_symbols: str
    t2_updown_window_minutes: str
    t2_updown_slots_ahead: int
    # T2 UPDOWN substrate (Phase 2): spot-anchored GBM pricing.
    # UPDOWN markets quote a much wider spread than the generic T2 gate allows
    # (empirically 250-408bps vs t2_max_spread_bps=120), but their fair value is
    # anchored on the underlying spot price (compute_fair_updown), NOT on the
    # orderbook mid — so a wide book spread does not mean "no edge". A dedicated,
    # looser spread ceiling is applied only to UPDOWN markets; the depth and
    # complement-error gates are unchanged. `t2_updown_spot_feed_enabled` toggles
    # the Binance spot WS feed that supplies s_now / ref_px / sigma; it defaults
    # to following `t2_updown_enabled` (no feed → no spot_fair → UPDOWN priced
    # like a generic binary, which is the Phase 1 behaviour).
    t2_updown_max_spread_bps: float
    t2_updown_spot_feed_enabled: bool
    # Comma-separated `symbol:binance_pair` overrides, e.g. "btc:BTCUSDT,eth:ETHUSDT".
    # Empty → built-in default mapping in spot_feed.py.
    t2_updown_spot_pairs: str
    # T2 extreme-price gate: reject directional entries at the tails of
    # [0, 1]. Empirical Polymarket data (Becker 2025, 72M trades) shows
    # BUY YES at price < 0.10 averaged -41% EV (longshot tax); the
    # symmetric BUY at price > 0.90 zone is "chasing a near-certain
    # event" with no real edge after fees. We refuse both sides.
    t2_reject_price_below: float
    t2_reject_price_above: float
    # T2 near-efficient category guard: in Finance / Crypto markets the
    # Becker 2025 maker-taker gap is only 0.17 pp — taker fees swamp any
    # statistical edge. We demand a much higher net_edge there (default
    # 300 bps, vs 25 bps for the rest).
    t2_near_efficient_min_net_edge_bps: float
    # Near-certainty rule (Article 4, Taleb / @stacyonchain). Markets
    # priced 92-98¢ may systematically underprice tail risk. Empirical
    # verification was blocked on free-tier data availability — see
    # `scripts/verify_near_certainty_trap.py`. Shipped in SHADOW MODE
    # by default: the rule computes what it would do on each signal
    # but does not modify production behaviour. Operators flip
    # `T2_NEAR_CERTAINTY_SHADOW_MODE=false` once enough live samples
    # have accumulated for an offline evaluation.
    t2_near_certainty_shadow_mode: bool
    t2_near_certainty_high_threshold: float  # default 0.92
    t2_near_certainty_low_threshold: float   # default 0.08
    t2_near_certainty_size_multiplier: float  # default 0.60
    t2_near_certainty_confidence_delta: float  # default -0.08
    # Barbell pool (Taleb / Article 4). Treat T2 capital as two
    # sub-buckets: data-driven (default ~80%) and tail (default ~15%,
    # with the rest as reserve). When enabled, the orchestrator
    # tracks per-class exposure and *relaxes* the tail_risk_high size
    # discount (e.g. 0.5 → 0.85) while the tail bucket has room. Once
    # the bucket is full, the discount snaps back to the harsh rule
    # default so we don't pile into correlated tail bets. Disabled by
    # default — operators flip on after observing the `barbell` block
    # under orchestrator.meta over a few days.
    t2_barbell_enabled: bool
    t2_barbell_tail_budget_pct: float       # of T2 allocation; default 0.15
    t2_barbell_tail_relaxed_multiplier: float  # default 0.85
    # T2 post-exit cooldown (cross-restart). After a successful exit or
    # abandoned-position release, the same market is locked out for
    # this many seconds so the bot does not immediately re-enter the
    # position the user (or the abandon path) just closed.
    t2_post_exit_cooldown_sec: float
    t2_recent_exits_state_file: str

    # T3 flow-bias data foundation (Becker 2025 follow-up). The bot
    # observes `last_trade_price` WS events, aggregates per-market
    # taker_yes_share over a sliding window, and surfaces it on T3
    # signals. Today the bias is telemetry-only; once the dataset is
    # large enough we wire it into actual quote-side selection.
    t3_flow_bias_enabled: bool
    t3_flow_bias_window_sec: float
    t3_flow_bias_min_trades: int
    t3_flow_bias_strong_threshold: float
    # How much weight the aggregated taker_yes_share gets when steering
    # maker quotes, as a fraction of `max_inventory`. 0.0 = telemetry
    # only (legacy); 0.5 = a fully one-sided market shifts quotes the
    # same as a half-loaded inventory book; 1.0 = same as a fully loaded
    # book. 0.5 is the safe initial value — strong enough to validate
    # the signal in shadow mode, weak enough that a bad flow window
    # can't completely cripple one side of the quote.
    t3_flow_bias_inventory_weight: float
    t3_flow_state_file: str

    # T3 maker exit policy. Pre-fix, maker fills had NO exit path —
    # a maker_crossed buy_yes at $0.23 on the Iran market just sat
    # there bleeding mark-to-market until resolution in 2027. These
    # mirror the T2 exit triggers but are simpler: no scale-out, no
    # optimal-stopping, no escalation ladder.
    #   maker_max_hold_sec: TTL stop (default 6h)
    #   maker_stop_loss_bps: adverse move vs entry VWAP (default 300)
    #   maker_take_profit_bps: favorable move captured (default 200)
    #   maker_exit_eval_interval_sec: per-position cooldown between
    #     evaluation passes, mirrors T2 (default 30)
    maker_max_hold_sec: float
    maker_stop_loss_bps: float
    maker_take_profit_bps: float
    maker_exit_eval_interval_sec: float

    # Shadow Mode (roadmap §三-阶段 1). Live trading is disabled while
    # `dry_run=True`; the engine instead simulates fills against the
    # cached orderbook and writes `virtual_fills.ndjson` so operators
    # can validate expected PnL / maker ratio / slippage before any
    # real capital is risked. `shadow_maker_fill_latency_sec` is the
    # minimum age a simulated maker quote must reach before it is
    # eligible to be marked FILLED by the cross-price sweep — keeps
    # the dataset honest about the queue position penalty.
    shadow_maker_fill_latency_sec: float

    # Tick 录制
    tick_record_enabled: bool
    tick_record_dir: str
    telemetry_record_enabled: bool
    telemetry_record_dir: str
    # P2-telemetry: when true, EventRecorder writes via a background
    # daemon thread so main-loop emit calls only pay JSON-encode +
    # queue-put cost (~µs) instead of write+fsync (~ms). Backpressure
    # policy: drop oldest events on queue overflow.
    telemetry_async_write: bool
    telemetry_async_queue_size: int

    # Data 清理
    data_cleanup_enabled: bool
    data_cleanup_interval_sec: float
    data_ticks_retention_days: int
    data_ticks_max_gb: float
    data_telemetry_retention_days: int
    data_telemetry_max_gb: float
    data_research_cache_retention_days: int
    data_research_cache_max_gb: float
    data_backtest_retention_days: int
    data_backtest_max_gb: float

    # Dashboard
    dashboard_enabled: bool
    dashboard_port: int

    # 日志
    log_level: str
    log_file: str

    # WebSocket 实时数据
    ws_enabled: bool
    ws_max_markets: int
    ws_refresh_cycles: int
    ws_vol_feed_interval_sec: float

    # LLM provider settings for offline/scanner tools.
    ai_provider: str
    ai_api_key: str
    ai_api_base: str
    ai_model: str
    ai_temperature: float

    # Research Signal
    research_signal_enabled: bool
    research_signal_window_sec: int
    research_signal_max_items: int
    research_signal_cache_ttl_sec: int
    research_signal_cache_dir: str
    research_signal_http_json_sources: str
    research_signal_feeds_file: str
    # Crypto macro-sentiment collector (Fear & Greed). Free, no auth.
    # Contributes a single sentiment row per crypto-keyword topic to
    # the research_overlay aggregator. Shadow-equivalent: it just
    # feeds the same pipeline as RSS / Surf / knowledge-base rows;
    # the orchestrator decides what to do with it via its resonance
    # scoring. Default on (no cost, low risk).
    research_signal_crypto_macro_enabled: bool
    research_signal_coingecko_enabled: bool
    research_signal_funding_rate_enabled: bool
    research_signal_econ_calendar_enabled: bool
    research_signal_defillama_enabled: bool
    research_signal_polymarket_activity_enabled: bool
    research_signal_manifold_enabled: bool

    # Backtest
    backtest_enabled: bool
    backtest_data_dir: str
    backtest_default_dataset: str
    backtest_slippage_bps: float
    backtest_reports_dir: str

    # New quant strategy gates (default off / inert unless configured)
    sniper_gate_enabled: bool = False
    sniper_min_net_edge_bps: float = 500.0
    sniper_min_confidence: float = 0.75
    sniper_min_liquidity: float = 0.0
    sniper_min_volume_24h: float = 0.0
    sniper_max_correlation_score: float = 0.80
    logical_constraints_json: str = ""
    event_baselines_json: str = ""
    wallet_alpha_profiles_json: str = ""
    wallet_alpha_observations_json: str = ""
    logical_constraints_file: str = ""
    event_baselines_file: str = ""
    wallet_alpha_profiles_file: str = ""
    wallet_alpha_observations_file: str = ""
    wallet_alpha_candidate_shadow_enabled: bool = False
    wallet_alpha_shadow_validation_enabled: bool = True
    wallet_alpha_shadow_max_signals_per_cycle: int = 5
    wallet_alpha_shadow_max_exec_ms_per_cycle: float = 250.0

    # Weather strategy (opt-in). Forecasts are external and must remain
    # shadow/dry-run until settlement data proves positive net expectancy.
    weather_strategy_enabled: bool = False
    weather_min_edge: float = 0.10
    weather_min_confidence: float = 0.70
    weather_max_spread_bps: float = 180.0
    weather_min_top_depth: float = 25.0
    weather_forecast_ttl_sec: float = 900.0
    weather_request_timeout_sec: float = 10.0
    weather_max_markets: int = 40

    # T3 流动性奖励带 (CLOB /rewards/markets/{condition_id})。
    # rewards_max_spread 以 cent 计价，换算成价格空间半宽 δ 后约束
    # maker 报价，使挂单真正落在计分区间内。关掉即回到"不知道奖励带"
    # 的历史行为（δ=0，纯 fair-value 报价）。
    maker_rewards_enabled: bool = True
    maker_rewards_ttl_sec: float = 900.0
    maker_rewards_negative_ttl_sec: float = 300.0
    maker_rewards_timeout_sec: float = 5.0
    # 单周期最多为多少个新市场拉取奖励参数（限制串行网络调用）。
    maker_rewards_prefetch_per_cycle: int = 20
    # true = 只在有奖励带的市场做市（默认 false，不改变现有覆盖面）。
    maker_rewards_only: bool = False

    # T3 挂单计分校验 (CLOB /orders-scoring)。挂在带内不等于真计分，
    # 这是"T3 到底有没有在赚奖励"的唯一客观指标。
    maker_scoring_audit_enabled: bool = True
    maker_scoring_audit_interval_sec: float = 60.0
    # 连续 grace 秒未计分的挂单是否撤掉（默认只观测不动作）。
    maker_scoring_cancel_unscored: bool = False
    maker_scoring_unscored_grace_sec: float = 90.0

    # user 频道 WebSocket（自己的订单/成交推送）。开启后成交在毫秒级
    # 回写风险敞口与退出管理器，而不是等下一个扫描周期的 REST 轮询。
    # REST 轮询保留作为兜底，两条路径走同一段落地逻辑。
    user_ws_enabled: bool = True
    user_ws_queue_size: int = 2000
    user_ws_max_events_per_cycle: int = 500

    # UPDOWN 结算口径现货源 (Polymarket RTDS crypto_prices)。
    # Binance 是流动性口径，UPDOWN 用的是 Polymarket 自己的价格源 ——
    # 两者的 basis 在剧烈波动时最大，正好是 UPDOWN 定价最敏感的时候。
    #   off     = 只用 Binance（接入前行为）
    #   shadow  = 定价仍走 Binance，RTDS 只产出 basis telemetry（默认）
    #   primary = 定价用 RTDS，过期时自动回落 Binance
    # 默认 shadow：crypto_prices 的 payload 字段名尚未拿到权威样本，
    # 先观测数据连续性再决定是否接管定价。
    t2_updown_rtds_mode: str = "shadow"
    t2_updown_rtds_staleness_sec: float = 30.0
    t2_updown_rtds_chainlink: bool = False
    t2_updown_basis_log_interval_sec: float = 300.0
    t2_updown_basis_alert_bps: float = 50.0

    # T1 跨平台配对的实体一致性否决。手写配对最危险的失败模式是"配错了"
    # 而不是"配漏了"：阈值 / 日期 / 方向不同的两个市场，poly_yes +
    # kalshi_no < 1 看起来仍像无风险套利，实际两条腿可能同时输。
    # 只否决不建对；任一侧缺问题原文就放行。
    cross_platform_entity_veto_enabled: bool = True
    # 词元 Jaccard 重叠下限。默认 0（关闭）：跨平台措辞差异很大但确实
    # 同一事件的配对很常见，用词重叠去否决容易误伤。
    cross_platform_min_token_overlap: float = 0.0

    # T3 抗狙击。做市的主要成本是逆向选择，不是 spread —— 跳变瞬间被扫、
    # 被单个异常 tick 牵着追价、刚被吃就原价补挂，是三个最常见的被吃场景。
    # 阈值用 tick 而不是 bps：1 个 tick 在 0.50 是 200 bps、在 0.05 是
    # 2000 bps，用 bps 设阈值会让低价市场永久暂停、高价市场形同虚设。
    t3_anti_snipe_enabled: bool = True
    t3_anti_snipe_jump_ticks: float = 3.0
    t3_anti_snipe_jump_pause_sec: float = 20.0
    t3_anti_snipe_stable_ticks_required: int = 2
    t3_anti_snipe_stable_band_ticks: float = 1.0
    t3_anti_snipe_ema_alpha: float = 0.3
    t3_anti_snipe_mid_history: int = 7
    t3_anti_snipe_post_fill_cooldown_sec: float = 15.0
    t3_anti_snipe_max_chase_ticks: float = 2.0

    def __post_init__(self) -> None:
        self.validate()

    def validate(self) -> None:
        if not self.clob_host or not self.gamma_host:
            raise ValueError("CLOB_HOST 和 GAMMA_HOST 不能为空")
        if self.clob_client_version not in {"auto", "v1", "v2"}:
            raise ValueError("POLYMARKET_CLOB_CLIENT_VERSION 必须是 auto / v1 / v2")
        api_cred_parts = [
            bool(self.clob_api_key),
            bool(self.clob_api_secret),
            bool(self.clob_api_passphrase),
        ]
        if any(api_cred_parts) and not all(api_cred_parts):
            raise ValueError("CLOB_API_KEY / CLOB_SECRET / CLOB_PASS_PHRASE 必须同时设置，或全部留空")
        if self.signature_type not in {0, 1, 2, 3}:
            raise ValueError("POLYMARKET_SIGNATURE_TYPE 必须是 0 / 1 / 2 / 3")
        if self.signature_type == 3 and self.clob_client_version == "v1":
            raise ValueError("POLYMARKET_SIGNATURE_TYPE=3 需要 POLYMARKET_CLOB_CLIENT_VERSION=auto 或 v2")
        if self.market_fetch_limit <= 0:
            raise ValueError("ARB_MARKET_FETCH_LIMIT 必须大于 0")
        if self.min_edge_usd < 0:
            raise ValueError("ARB_MIN_EDGE_USD 不能为负数")
        if self.min_edge_pct < 0:
            raise ValueError("ARB_MIN_EDGE_PCT 不能为负数")
        if self.max_order_size_usdc <= 0:
            raise ValueError("ARB_MAX_ORDER_SIZE_USDC 必须大于 0")
        if self.default_order_size_usdc <= 0:
            raise ValueError("ARB_DEFAULT_ORDER_SIZE_USDC 必须大于 0")
        if self.default_order_size_usdc > self.max_order_size_usdc:
            raise ValueError("ARB_DEFAULT_ORDER_SIZE_USDC 不能大于 ARB_MAX_ORDER_SIZE_USDC")
        if self.live_max_order_size_usdc <= 0:
            raise ValueError("LIVE_MAX_ORDER_SIZE_USDC 必须大于 0")
        if self.live_max_total_exposure_usdc <= 0:
            raise ValueError("LIVE_MAX_TOTAL_EXPOSURE_USDC 必须大于 0")
        if self.live_min_net_edge_bps < 0:
            raise ValueError("LIVE_MIN_NET_EDGE_BPS 不能为负数")
        if self.live_min_net_edge_usd < 0:
            raise ValueError("LIVE_MIN_NET_EDGE_USD 不能为负数")
        if self.live_max_orderbook_snapshot_age_sec < 0:
            raise ValueError("LIVE_MAX_ORDERBOOK_SNAPSHOT_AGE_SEC 不能为负数")
        if not 0 <= self.live_min_ws_hit_ratio <= 1:
            raise ValueError("LIVE_MIN_WS_HIT_RATIO 必须在 [0, 1] 区间")
        if self.min_liquidity < 0 or self.min_volume_24h < 0:
            raise ValueError("ARB_MIN_LIQUIDITY 和 ARB_MIN_VOLUME_24H 不能为负数")
        if not 0 <= self.polymarket_taker_fee_rate < 1:
            raise ValueError("POLYMARKET_TAKER_FEE_RATE 必须在 [0, 1) 区间")
        if not 0 <= self.kalshi_taker_fee_rate < 1:
            raise ValueError("KALSHI_TAKER_FEE_RATE 必须在 [0, 1) 区间")
        if not 0 <= self.weather_min_edge < 1:
            raise ValueError("WEATHER_MIN_EDGE 必须在 [0, 1) 区间")
        if not 0 <= self.weather_min_confidence <= 1:
            raise ValueError("WEATHER_MIN_CONFIDENCE 必须在 [0, 1] 区间")
        if self.weather_max_spread_bps < 0 or self.weather_min_top_depth < 0:
            raise ValueError("WEATHER_MAX_SPREAD_BPS 和 WEATHER_MIN_TOP_DEPTH 不能为负数")
        if self.weather_forecast_ttl_sec <= 0 or self.weather_request_timeout_sec <= 0:
            raise ValueError("天气预测缓存和请求超时必须大于 0")
        if self.weather_max_markets < 0:
            raise ValueError("WEATHER_MAX_MARKETS 不能为负数")
        if self.market_universe_refresh_sec <= 0:
            raise ValueError("ARB_MARKET_UNIVERSE_REFRESH_SEC 必须大于 0")
        if self.hot_market_pool_size <= 0:
            raise ValueError("ARB_HOT_MARKET_POOL_SIZE 必须大于 0")
        if self.hot_event_pool_size <= 0:
            raise ValueError("ARB_HOT_EVENT_POOL_SIZE 必须大于 0")
        if self.dirty_market_wake_threshold < 1:
            raise ValueError("dirty_market_wake_threshold 必须 >= 1")
        if self.scan_interval_sec <= 0:
            raise ValueError("ARB_SCAN_INTERVAL_SEC 必须大于 0")
        if self.orderbook_retry_count < 0:
            raise ValueError("ORDERBOOK_RETRY_COUNT 不能为负数")
        if self.orderbook_ws_snapshot_max_age_sec < 0:
            raise ValueError("ORDERBOOK_WS_SNAPSHOT_MAX_AGE_SEC 不能为负数")
        if self.orderbook_retry_delay_sec < 0:
            raise ValueError("ORDERBOOK_RETRY_DELAY_SEC 不能为负数")
        if self.orderbook_missing_cooldown_sec < 0:
            raise ValueError("ORDERBOOK_MISSING_COOLDOWN_SEC 不能为负数")
        if self.orderbook_batch_concurrency < 1:
            raise ValueError("ORDERBOOK_BATCH_CONCURRENCY 必须至少为 1")
        if self.max_multi_outcome_legs < 2:
            raise ValueError("ARB_MAX_MULTI_OUTCOME_LEGS 必须至少为 2")
        if self.max_open_positions <= 0:
            raise ValueError("RISK_MAX_OPEN_POSITIONS 必须大于 0")
        if self.max_exposure_per_market <= 0 or self.max_total_exposure <= 0:
            raise ValueError("风险敞口上限必须大于 0")
        if self.max_daily_loss < 0:
            raise ValueError("RISK_MAX_DAILY_LOSS 不能为负数")
        if self.risk_event_cooldown_sec < 0:
            raise ValueError("RISK_EVENT_COOLDOWN_SEC 不能为负数")
        if self.risk_pending_reservation_ttl_sec < 0:
            raise ValueError("RISK_PENDING_RESERVATION_TTL_SEC 不能为负数")
        if self.risk_halt_auto_recover_sec < 0:
            raise ValueError("RISK_HALT_AUTO_RECOVER_SEC 不能为负数")
        if self.maker_stale_order_ttl_sec < 0:
            raise ValueError("MAKER_STALE_ORDER_TTL_SEC 不能为负数")
        if self.portfolio_sync_enabled and self.portfolio_sync_interval_sec <= 0:
            raise ValueError("PORTFOLIO_SYNC_INTERVAL_SEC 必须大于 0")
        if self.portfolio_sync_enabled and self.portfolio_sync_timeout_sec <= 0:
            raise ValueError("PORTFOLIO_SYNC_TIMEOUT_SEC 必须大于 0")
        if self.portfolio_sync_enabled and not self.data_api_host:
            raise ValueError("DATA_API_HOST 不能为空")
        if self.portfolio_sync_max_consecutive_failures < 0:
            raise ValueError("PORTFOLIO_SYNC_MAX_CONSECUTIVE_FAILURES 不能为负数")
        if self.notification_cooldown_sec < 0:
            raise ValueError("NOTIFICATION_COOLDOWN_SEC 不能为负数")
        feishu_configured = bool(self.feishu_app_id or self.feishu_app_secret or self.feishu_open_id)
        if feishu_configured:
            if not self.feishu_app_id or not self.feishu_app_secret:
                raise ValueError("启用飞书通知需要 FEISHU_APP_ID 和 FEISHU_APP_SECRET")
            if not self.feishu_open_id:
                raise ValueError("启用飞书通知需要 FEISHU_OPEN_ID")
        if self.pnl_profit_alert_usdc < 0 or self.pnl_loss_alert_usdc < 0:
            raise ValueError("PNL 告警阈值不能为负数")
        if self.fatal_error_cooldown_sec < 0:
            raise ValueError("FATAL_ERROR_COOLDOWN_SEC 不能为负数")
        hhmm = self.daily_summary_time_hhmm.strip()
        if hhmm:
            try:
                hour_text, minute_text = hhmm.split(":", 1)
                hour = int(hour_text)
                minute = int(minute_text)
            except ValueError as exc:
                raise ValueError("DAILY_SUMMARY_TIME_HHMM 必须是 HH:MM 格式") from exc
            if not (0 <= hour <= 23 and 0 <= minute <= 59):
                raise ValueError("DAILY_SUMMARY_TIME_HHMM 必须在 00:00-23:59 之间")
        if self.daily_summary_timezone:
            try:
                ZoneInfo(self.daily_summary_timezone)
            except Exception as exc:
                raise ValueError(f"DAILY_SUMMARY_TIMEZONE 无效: {self.daily_summary_timezone}") from exc
        if self.data_cleanup_enabled and self.data_cleanup_interval_sec <= 0:
            raise ValueError("DATA_CLEANUP_INTERVAL_SEC 必须大于 0")
        if min(
            self.data_ticks_retention_days,
            self.data_telemetry_retention_days,
            self.data_research_cache_retention_days,
            self.data_backtest_retention_days,
        ) < 0:
            raise ValueError("数据保留天数不能为负数")
        if min(
            self.data_ticks_max_gb,
            self.data_telemetry_max_gb,
            self.data_research_cache_max_gb,
            self.data_backtest_max_gb,
        ) < 0:
            raise ValueError("数据目录容量上限不能为负数")
        if not 0 <= self.edge_min_confidence <= 1:
            raise ValueError("EDGE_MIN_CONFIDENCE 必须在 [0, 1] 区间")
        if self.edge_confidence_full_bps <= 0:
            raise ValueError("EDGE_CONFIDENCE_FULL_BPS 必须大于 0")
        if self.edge_confidence_imbalance_weight < 0:
            raise ValueError("EDGE_CONFIDENCE_IMBALANCE_WEIGHT 不能为负数")
        if self.edge_volatility_spike_ratio <= 0 or self.edge_volatility_calm_ratio <= 0:
            raise ValueError("波动率比率阈值必须大于 0")
        if self.edge_volatility_spike_penalty <= 0 or self.edge_volatility_calm_boost <= 0:
            raise ValueError("波动率置信度调整因子必须大于 0")
        if self.t2_min_deviation < 0:
            raise ValueError("T2_MIN_DEVIATION 不能为负数")
        if self.t2_max_spread_bps < 0:
            raise ValueError("T2_MAX_SPREAD_BPS 不能为负数")
        if self.t2_min_top_depth < 0:
            raise ValueError("T2_MIN_TOP_DEPTH 不能为负数")
        if self.t2_max_complement_error_bps < 0:
            raise ValueError("T2_MAX_COMPLEMENT_ERROR_BPS 不能为负数")
        if self.t2_max_signals_per_market_per_hour < 0:
            raise ValueError("T2_MAX_SIGNALS_PER_MARKET_PER_HOUR 不能为负数")
        if self.t2_stop_loss_bps < 0:
            raise ValueError("T2_STOP_LOSS_BPS 不能为负数")
        if self.t2_take_profit_capture_pct < 0:
            raise ValueError("T2_TAKE_PROFIT_CAPTURE_PCT 不能为负数")
        if self.t2_max_hold_sec < 0:
            raise ValueError("T2_MAX_HOLD_SEC 不能为负数")
        if self.t2_exit_eval_interval_sec < 0:
            raise ValueError("T2_EXIT_EVAL_INTERVAL_SEC 不能为负数")
        if self.maker_max_hold_sec < 0:
            raise ValueError("MAKER_MAX_HOLD_SEC 不能为负数")
        if self.maker_stop_loss_bps < 0:
            raise ValueError("MAKER_STOP_LOSS_BPS 不能为负数")
        if self.maker_take_profit_bps < 0:
            raise ValueError("MAKER_TAKE_PROFIT_BPS 不能为负数")
        if self.maker_exit_eval_interval_sec < 0:
            raise ValueError("MAKER_EXIT_EVAL_INTERVAL_SEC 不能为负数")
        if self.maker_rewards_ttl_sec < 0:
            raise ValueError("MAKER_REWARDS_TTL_SEC 不能为负数")
        if self.maker_rewards_negative_ttl_sec < 0:
            raise ValueError("MAKER_REWARDS_NEGATIVE_TTL_SEC 不能为负数")
        if self.maker_rewards_timeout_sec <= 0:
            raise ValueError("MAKER_REWARDS_TIMEOUT_SEC 必须大于 0")
        if self.maker_rewards_prefetch_per_cycle < 0:
            raise ValueError("MAKER_REWARDS_PREFETCH_PER_CYCLE 不能为负数")
        if self.maker_scoring_audit_interval_sec < 0:
            raise ValueError("MAKER_SCORING_AUDIT_INTERVAL_SEC 不能为负数")
        if self.maker_scoring_unscored_grace_sec < 0:
            raise ValueError("MAKER_SCORING_UNSCORED_GRACE_SEC 不能为负数")
        if self.user_ws_queue_size < 1:
            raise ValueError("USER_WS_QUEUE_SIZE 必须 >= 1")
        if self.user_ws_max_events_per_cycle < 1:
            raise ValueError("USER_WS_MAX_EVENTS_PER_CYCLE 必须 >= 1")
        if self.t2_updown_rtds_mode not in ("off", "shadow", "primary"):
            raise ValueError("T2_UPDOWN_RTDS_MODE 必须是 off / shadow / primary")
        if self.t2_updown_rtds_staleness_sec < 0:
            raise ValueError("T2_UPDOWN_RTDS_STALENESS_SEC 不能为负数")
        if self.t2_updown_basis_log_interval_sec < 0:
            raise ValueError("T2_UPDOWN_BASIS_LOG_INTERVAL_SEC 不能为负数")
        if self.t2_updown_basis_alert_bps < 0:
            raise ValueError("T2_UPDOWN_BASIS_ALERT_BPS 不能为负数")
        if not (0.0 <= self.cross_platform_min_token_overlap <= 1.0):
            raise ValueError("CROSS_PLATFORM_MIN_TOKEN_OVERLAP 必须在 [0, 1]")
        if self.t3_anti_snipe_jump_ticks < 0:
            raise ValueError("T3_ANTI_SNIPE_JUMP_TICKS 不能为负数")
        if self.t3_anti_snipe_jump_pause_sec < 0:
            raise ValueError("T3_ANTI_SNIPE_JUMP_PAUSE_SEC 不能为负数")
        if self.t3_anti_snipe_stable_ticks_required < 0:
            raise ValueError("T3_ANTI_SNIPE_STABLE_TICKS_REQUIRED 不能为负数")
        if self.t3_anti_snipe_stable_band_ticks < 0:
            raise ValueError("T3_ANTI_SNIPE_STABLE_BAND_TICKS 不能为负数")
        if not (0.0 <= self.t3_anti_snipe_ema_alpha <= 1.0):
            raise ValueError("T3_ANTI_SNIPE_EMA_ALPHA 必须在 [0, 1]")
        if self.t3_anti_snipe_mid_history < 1:
            raise ValueError("T3_ANTI_SNIPE_MID_HISTORY 必须 >= 1")
        if self.t3_anti_snipe_post_fill_cooldown_sec < 0:
            raise ValueError("T3_ANTI_SNIPE_POST_FILL_COOLDOWN_SEC 不能为负数")
        if self.t3_anti_snipe_max_chase_ticks < 0:
            raise ValueError("T3_ANTI_SNIPE_MAX_CHASE_TICKS 不能为负数")
        if self.t2_scale_out_tranches < 1:
            raise ValueError("T2_SCALE_OUT_TRANCHES 必须 >= 1")
        if self.t2_stop_loss_dynamic_k < 0:
            raise ValueError("T2_STOP_LOSS_DYNAMIC_K 不能为负数")
        if self.t2_stop_loss_min_bps < 0:
            raise ValueError("T2_STOP_LOSS_MIN_BPS 不能为负数")
        if self.t2_stop_loss_max_bps < self.t2_stop_loss_min_bps:
            raise ValueError("T2_STOP_LOSS_MAX_BPS 必须 >= T2_STOP_LOSS_MIN_BPS")
        if self.t2_stop_loss_dynamic_warmup < 2:
            raise ValueError("T2_STOP_LOSS_DYNAMIC_WARMUP 必须 >= 2")
        if self.t2_long_horizon_days < 0:
            raise ValueError("T2_LONG_HORIZON_DAYS 不能为负数")
        if self.t2_long_horizon_min_net_edge_bps < 0:
            raise ValueError("T2_LONG_HORIZON_MIN_NET_EDGE_BPS 不能为负数")
        if self.t2_max_horizon_days < 0:
            raise ValueError("T2_MAX_HORIZON_DAYS 不能为负数")
        if self.t2_max_signals_per_cycle < 0:
            raise ValueError("T2_MAX_SIGNALS_PER_CYCLE 不能为负数")
        if self.t2_updown_priority_boost < 0:
            raise ValueError("T2_UPDOWN_PRIORITY_BOOST 不能为负数")
        if self.t2_updown_slots_ahead < 0:
            raise ValueError("T2_UPDOWN_SLOTS_AHEAD 不能为负数")
        if self.t2_updown_max_spread_bps < 0:
            raise ValueError("T2_UPDOWN_MAX_SPREAD_BPS 不能为负数")
        if not (0.0 <= self.t2_reject_price_below <= 0.5):
            raise ValueError("T2_REJECT_PRICE_BELOW 必须在 [0, 0.5]")
        if not (0.5 <= self.t2_reject_price_above <= 1.0):
            raise ValueError("T2_REJECT_PRICE_ABOVE 必须在 [0.5, 1.0]")
        if self.t2_near_efficient_min_net_edge_bps < 0:
            raise ValueError("T2_NEAR_EFFICIENT_MIN_NET_EDGE_BPS 不能为负数")
        if not (0.5 < self.t2_near_certainty_high_threshold <= 1.0):
            raise ValueError("T2_NEAR_CERTAINTY_HIGH_THRESHOLD 必须在 (0.5, 1.0]")
        if not (0.0 <= self.t2_near_certainty_low_threshold < 0.5):
            raise ValueError("T2_NEAR_CERTAINTY_LOW_THRESHOLD 必须在 [0, 0.5)")
        if not (0.0 < self.t2_near_certainty_size_multiplier <= 1.0):
            raise ValueError("T2_NEAR_CERTAINTY_SIZE_MULTIPLIER 必须在 (0, 1]")
        if not (0.0 <= self.t2_barbell_tail_budget_pct <= 1.0):
            raise ValueError("T2_BARBELL_TAIL_BUDGET_PCT 必须在 [0, 1]")
        if not (0.0 < self.t2_barbell_tail_relaxed_multiplier <= 1.0):
            raise ValueError("T2_BARBELL_TAIL_RELAXED_MULTIPLIER 必须在 (0, 1]")
        if self.t2_post_exit_cooldown_sec < 0:
            raise ValueError("T2_POST_EXIT_COOLDOWN_SEC 不能为负数")
        if self.t3_flow_bias_window_sec <= 0:
            raise ValueError("T3_FLOW_BIAS_WINDOW_SEC 必须大于 0")
        if self.t3_flow_bias_min_trades < 1:
            raise ValueError("T3_FLOW_BIAS_MIN_TRADES 必须 >= 1")
        if not (0.5 <= self.t3_flow_bias_strong_threshold <= 1.0):
            raise ValueError("T3_FLOW_BIAS_STRONG_THRESHOLD 必须在 [0.5, 1.0]")
        if self.t3_flow_bias_inventory_weight < 0:
            raise ValueError("T3_FLOW_BIAS_INVENTORY_WEIGHT 不能为负数")
        if self.shadow_maker_fill_latency_sec < 0:
            raise ValueError("SHADOW_MAKER_FILL_LATENCY_SEC 不能为负数")
        if self.sniper_min_net_edge_bps < 0:
            raise ValueError("SNIPER_MIN_NET_EDGE_BPS 不能为负数")
        if not 0 <= self.sniper_min_confidence <= 1:
            raise ValueError("SNIPER_MIN_CONFIDENCE 必须在 [0, 1] 区间")
        if self.sniper_min_liquidity < 0 or self.sniper_min_volume_24h < 0:
            raise ValueError("SNIPER_MIN_LIQUIDITY 和 SNIPER_MIN_VOLUME_24H 不能为负数")
        if not 0 <= self.sniper_max_correlation_score <= 1:
            raise ValueError("SNIPER_MAX_CORRELATION_SCORE 必须在 [0, 1] 区间")
        if self.wallet_alpha_shadow_max_signals_per_cycle < 0:
            raise ValueError("WALLET_ALPHA_SHADOW_MAX_SIGNALS_PER_CYCLE 不能为负数")
        if self.wallet_alpha_shadow_max_exec_ms_per_cycle < 0:
            raise ValueError("WALLET_ALPHA_SHADOW_MAX_EXEC_MS_PER_CYCLE 不能为负数")
        if (
            self.portfolio_sync_enabled
            and not self.portfolio_sync_user_address
            and self.funder_address == _NO_WALLET_PLACEHOLDER
        ):
            raise ValueError("PORTFOLIO_SYNC_ENABLED=true 需要设置 POLYMARKET_FUNDER 或 PORTFOLIO_SYNC_USER_ADDRESS")
        if not self.dry_run:
            if not self.live_trading_ack:
                raise ValueError("实盘前必须设置 LIVE_TRADING_ACK=true")
            if self.live_require_portfolio_sync and not self.portfolio_sync_enabled:
                raise ValueError("实盘前必须启用 PORTFOLIO_SYNC_ENABLED=true 或设置 LIVE_REQUIRE_PORTFOLIO_SYNC=false")
            if not self.live_allow_zero_taker_fee and self.polymarket_taker_fee_rate <= 0:
                raise ValueError("实盘前 POLYMARKET_TAKER_FEE_RATE 不能为 0，除非设置 LIVE_ALLOW_ZERO_TAKER_FEE=true")
            if self.max_order_size_usdc > self.live_max_order_size_usdc:
                raise ValueError("ARB_MAX_ORDER_SIZE_USDC 超过 LIVE_MAX_ORDER_SIZE_USDC")
            if self.max_total_exposure > self.live_max_total_exposure_usdc:
                raise ValueError("RISK_MAX_TOTAL_EXPOSURE 超过 LIVE_MAX_TOTAL_EXPOSURE_USDC")

    @classmethod
    def from_env(
        cls,
        dotenv_path: str | Path | None = None,
        *,
        require_wallet: bool | None = None,
    ) -> ArbConfig:
        """从 .env 文件和环境变量构建配置.

        require_wallet=None 表示仅在 ARB_DRY_RUN=false（实盘）时要求钱包。
        """
        if str(dotenv_path or "") == "__ENV_ONLY__":
            pass
        elif dotenv_path:
            load_dotenv(dotenv_path, override=True)
        else:
            load_dotenv(override=True)

        if require_wallet is None:
            require_wallet = not _env_bool("ARB_DRY_RUN", True)
        private_key = _env("PRIVATE_KEY") or _env("POLYMARKET_PRIVATE_KEY")
        funder = _env("POLYMARKET_FUNDER") or _env("POLYMARKET_DEPOSIT_WALLET")
        if require_wallet:
            if not private_key:
                raise ValueError("必须设置 PRIVATE_KEY 或 POLYMARKET_PRIVATE_KEY")
            if not funder:
                raise ValueError("必须设置 POLYMARKET_FUNDER")
        else:
            private_key = private_key or _NO_WALLET_PLACEHOLDER
            funder = funder or _NO_WALLET_PLACEHOLDER

        cfg = cls(
            private_key=private_key,
            funder_address=funder,
            signature_type=_env_int("POLYMARKET_SIGNATURE_TYPE", 2),
            chain_id=_env_int("CHAIN_ID", 137),
            clob_host=_env("CLOB_HOST", "https://clob.polymarket.com"),
            gamma_host=_env("GAMMA_HOST", "https://gamma-api.polymarket.com"),
            clob_client_version=_env("POLYMARKET_CLOB_CLIENT_VERSION", "auto").lower() or "auto",
            clob_api_key=_env("CLOB_API_KEY") or _env("POLYMARKET_CLOB_API_KEY"),
            clob_api_secret=_env("CLOB_SECRET") or _env("CLOB_API_SECRET") or _env("POLYMARKET_CLOB_SECRET"),
            clob_api_passphrase=(
                _env("CLOB_PASS_PHRASE")
                or _env("CLOB_API_PASSPHRASE")
                or _env("CLOB_API_PASS_PHRASE")
                or _env("POLYMARKET_CLOB_PASS_PHRASE")
            ),
            min_edge_usd=_env_float("ARB_MIN_EDGE_USD", 0.005),
            min_edge_pct=_env_float("ARB_MIN_EDGE_PCT", 0.3),
            max_order_size_usdc=_env_float("ARB_MAX_ORDER_SIZE_USDC", 50.0),
            default_order_size_usdc=_env_float("ARB_DEFAULT_ORDER_SIZE_USDC", 10.0),
            scan_interval_sec=_env_float("ARB_SCAN_INTERVAL_SEC", 5.0),
            dirty_market_wake_threshold=_env_int("DIRTY_MARKET_WAKE_THRESHOLD", 1),
            market_fetch_limit=_env_int("ARB_MARKET_FETCH_LIMIT", 100),
            market_universe_refresh_sec=_env_float("ARB_MARKET_UNIVERSE_REFRESH_SEC", 600.0),
            hot_market_pool_size=_env_int("ARB_HOT_MARKET_POOL_SIZE", 80),
            hot_event_pool_size=_env_int("ARB_HOT_EVENT_POOL_SIZE", 30),
            market_focus_keywords=_env("ARB_MARKET_FOCUS_KEYWORDS", ""),
            dry_run=_env_bool("ARB_DRY_RUN", True),
            live_trading_ack=_env_bool("LIVE_TRADING_ACK", False),
            live_require_portfolio_sync=_env_bool("LIVE_REQUIRE_PORTFOLIO_SYNC", True),
            live_allow_zero_taker_fee=_env_bool("LIVE_ALLOW_ZERO_TAKER_FEE", False),
            live_max_order_size_usdc=_env_float("LIVE_MAX_ORDER_SIZE_USDC", 10.0),
            live_max_total_exposure_usdc=_env_float("LIVE_MAX_TOTAL_EXPOSURE_USDC", 100.0),
            live_min_net_edge_bps=_env_float("LIVE_MIN_NET_EDGE_BPS", 25.0),
            live_min_net_edge_usd=_env_float("LIVE_MIN_NET_EDGE_USD", 0.0025),
            # Raised from 1.0 → 5.0 to match the WS snapshot acceptance
            # window (`ORDERBOOK_WS_SNAPSHOT_MAX_AGE_SEC` default 10s)
            # more reasonably. Polymarket only pushes on price changes,
            # so a quiet token can legitimately sit untouched for several
            # seconds without being "stale". 1s was both contradictory
            # (single-point reads accepted up to 10s) and aggressive
            # enough to block T2 entirely whenever one mirror token went
            # quiet. Operators who want stricter live freshness can
            # still lower this via env.
            live_max_orderbook_snapshot_age_sec=_env_float("LIVE_MAX_ORDERBOOK_SNAPSHOT_AGE_SEC", 5.0),
            live_min_ws_hit_ratio=_env_float("LIVE_MIN_WS_HIT_RATIO", 0.25),
            maker_strategy_enabled=_env_bool("MAKER_STRATEGY_ENABLED", False),
            min_liquidity=_env_float("ARB_MIN_LIQUIDITY", 1000.0),
            min_volume_24h=_env_float("ARB_MIN_VOLUME_24H", 500.0),
            # REST cache TTL. With a large market universe most tokens
            # never land in the WS mirror, so every get_snapshot() call for
            # them is a REST round trip; 2.5s was still shorter than a
            # single real scan cycle (observed ~800s+ under a ~600-market
            # universe with only ~100 WS-covered tokens), so no snapshot
            # fetched anywhere in a cycle was ever reused. 5s matches
            # LIVE_MAX_ORDERBOOK_SNAPSHOT_AGE_SEC, so no staleness beyond
            # what live already tolerates; T0 reads the WS mirror first.
            orderbook_snapshot_ttl_sec=_env_float("ORDERBOOK_SNAPSHOT_TTL_SEC", 5.0),
            orderbook_ws_snapshot_max_age_sec=_env_float("ORDERBOOK_WS_SNAPSHOT_MAX_AGE_SEC", 10.0),
            orderbook_retry_count=_env_int("ORDERBOOK_RETRY_COUNT", 2),
            orderbook_retry_delay_sec=_env_float("ORDERBOOK_RETRY_DELAY_SEC", 0.15),
            orderbook_missing_cooldown_sec=_env_float("ORDERBOOK_MISSING_COOLDOWN_SEC", 300.0),
            # REST fallback for batch_get_snapshots was strictly serial —
            # on a cold WS mirror this meant hundreds of sequential HTTP
            # round trips per cycle. Fetch concurrently instead.
            orderbook_batch_concurrency=_env_int("ORDERBOOK_BATCH_CONCURRENCY", 16),
            cross_platform_pairs_json=_env("CROSS_PLATFORM_PAIRS_JSON", ""),
            # Polymarket's current binary taker baseline is 0.5%. Keep the
            # fallback aligned with `.env.example`; a 5% fallback silently
            # changes signal admission and makes offline/live results diverge
            # by an order of magnitude.
            polymarket_taker_fee_rate=_env_float("POLYMARKET_TAKER_FEE_RATE", 0.005),
            kalshi_taker_fee_rate=_env_float("KALSHI_TAKER_FEE_RATE", 0.003),
            max_multi_outcome_legs=_env_int("ARB_MAX_MULTI_OUTCOME_LEGS", 20),
            t0_min_multi_outcome_median_leg_price=_env_float("T0_MIN_MULTI_OUTCOME_MEDIAN_LEG_PRICE", 0.05),
            t0_require_complete_partition=_env_bool("T0_REQUIRE_COMPLETE_PARTITION", True),
            max_open_positions=_env_int("RISK_MAX_OPEN_POSITIONS", 10),
            max_exposure_per_market=_env_float("RISK_MAX_EXPOSURE_PER_MARKET", 100.0),
            max_total_exposure=_env_float("RISK_MAX_TOTAL_EXPOSURE", 500.0),
            max_daily_loss=_env_float("RISK_MAX_DAILY_LOSS", 50.0),
            max_consecutive_failures=_env_int("RISK_MAX_CONSECUTIVE_FAILURES", 5),
            risk_event_cooldown_sec=_env_float("RISK_EVENT_COOLDOWN_SEC", 60.0),
            risk_pending_reservation_ttl_sec=_env_float("RISK_PENDING_RESERVATION_TTL_SEC", 30.0),
            risk_halt_auto_recover_sec=_env_float("RISK_HALT_AUTO_RECOVER_SEC", 3600.0),
            maker_stale_order_ttl_sec=_env_float("MAKER_STALE_ORDER_TTL_SEC", 60.0),
            portfolio_sync_enabled=_env_bool("PORTFOLIO_SYNC_ENABLED", False),
            portfolio_sync_interval_sec=_env_float("PORTFOLIO_SYNC_INTERVAL_SEC", 60.0),
            portfolio_sync_timeout_sec=_env_float("PORTFOLIO_SYNC_TIMEOUT_SEC", 5.0),
            portfolio_sync_max_consecutive_failures=_env_int("PORTFOLIO_SYNC_MAX_CONSECUTIVE_FAILURES", 3),
            data_api_host=_env("DATA_API_HOST", "https://data-api.polymarket.com"),
            portfolio_sync_user_address=_env("PORTFOLIO_SYNC_USER_ADDRESS"),
            feishu_app_id=_env("FEISHU_APP_ID"),
            feishu_app_secret=_env("FEISHU_APP_SECRET"),
            feishu_open_id=_env("FEISHU_OPEN_ID"),
            feishu_api_base=_env("FEISHU_API_BASE", "https://open.feishu.cn/open-apis"),
            notification_cooldown_sec=_env_float("NOTIFICATION_COOLDOWN_SEC", 30.0),
            notify_on_arb_found=_env_bool("NOTIFY_ON_ARB_FOUND", False),
            notify_arb_found_in_shadow=_env_bool("NOTIFY_ARB_FOUND_IN_SHADOW", False),
            notify_on_trade_success=_env_bool("NOTIFY_ON_TRADE_SUCCESS", True),
            notify_on_trade_failure=_env_bool("NOTIFY_ON_TRADE_FAILURE", True),
            notify_on_fatal_error=_env_bool("NOTIFY_ON_FATAL_ERROR", True),
            notify_on_pnl_alert=_env_bool("NOTIFY_ON_PNL_ALERT", True),
            notify_on_daily_summary=_env_bool("NOTIFY_ON_DAILY_SUMMARY", True),
            pnl_profit_alert_usdc=_env_float("PNL_PROFIT_ALERT_USDC", 20.0),
            pnl_loss_alert_usdc=_env_float("PNL_LOSS_ALERT_USDC", 10.0),
            fatal_error_cooldown_sec=_env_float("FATAL_ERROR_COOLDOWN_SEC", 3600.0),
            daily_summary_time_hhmm=_env("DAILY_SUMMARY_TIME_HHMM", "08:05"),
            daily_summary_timezone=_env("DAILY_SUMMARY_TIMEZONE", "Asia/Shanghai"),
            notification_state_file=_env("NOTIFICATION_STATE_FILE", "data/telemetry/notification_state.json"),
            vol_fast_minutes=_env_int("VOL_FAST_MINUTES", 60),
            vol_slow_minutes=_env_int("VOL_SLOW_MINUTES", 360),
            vol_min_bars=_env_int("VOL_MIN_BARS", 20),
            edge_min_bps=_env_float("EDGE_MIN_BPS", 100.0),
            edge_max_spread_bps=_env_float("EDGE_MAX_SPREAD_BPS", 500.0),
            edge_min_confidence=_env_float("EDGE_MIN_CONFIDENCE", 0.4),
            edge_confidence_full_bps=_env_float("EDGE_CONFIDENCE_FULL_BPS", 500.0),
            edge_confidence_imbalance_weight=_env_float("EDGE_CONFIDENCE_IMBALANCE_WEIGHT", 0.1),
            edge_volatility_spike_ratio=_env_float("EDGE_VOLATILITY_SPIKE_RATIO", 2.0),
            edge_volatility_spike_penalty=_env_float("EDGE_VOLATILITY_SPIKE_PENALTY", 0.7),
            edge_volatility_calm_ratio=_env_float("EDGE_VOLATILITY_CALM_RATIO", 0.8),
            edge_volatility_calm_boost=_env_float("EDGE_VOLATILITY_CALM_BOOST", 1.1),
            t2_min_deviation=_env_float("T2_MIN_DEVIATION", 0.05),
            t2_max_spread_bps=_env_float("T2_MAX_SPREAD_BPS", 80.0),
            t2_min_top_depth=_env_float("T2_MIN_TOP_DEPTH", 100.0),
            t2_max_complement_error_bps=_env_float("T2_MAX_COMPLEMENT_ERROR_BPS", 150.0),
            t2_max_signals_per_market_per_hour=_env_int("T2_MAX_SIGNALS_PER_MARKET_PER_HOUR", 2),
            t2_stop_loss_bps=_env_float("T2_STOP_LOSS_BPS", 300.0),
            t2_take_profit_capture_pct=_env_float("T2_TAKE_PROFIT_CAPTURE_PCT", 0.6),
            t2_max_hold_sec=_env_float("T2_MAX_HOLD_SEC", 6 * 3600.0),
            t2_exit_eval_interval_sec=_env_float("T2_EXIT_EVAL_INTERVAL_SEC", 30.0),
            t2_optimal_stopping_enabled=_env_bool("T2_OPTIMAL_STOPPING_ENABLED", True),
            t2_scale_out_tranches=_env_int("T2_SCALE_OUT_TRANCHES", 3),
            t2_stop_loss_dynamic_enabled=_env_bool("T2_STOP_LOSS_DYNAMIC_ENABLED", False),
            t2_stop_loss_dynamic_k=_env_float("T2_STOP_LOSS_DYNAMIC_K", 2.0),
            t2_stop_loss_min_bps=_env_float("T2_STOP_LOSS_MIN_BPS", 100.0),
            t2_stop_loss_max_bps=_env_float("T2_STOP_LOSS_MAX_BPS", 1000.0),
            t2_stop_loss_dynamic_warmup=_env_int("T2_STOP_LOSS_DYNAMIC_WARMUP", 5),
            t2_long_horizon_days=_env_float("T2_LONG_HORIZON_DAYS", 30.0),
            t2_long_horizon_min_net_edge_bps=_env_float("T2_LONG_HORIZON_MIN_NET_EDGE_BPS", 200.0),
            t2_max_horizon_days=_env_float("T2_MAX_HORIZON_DAYS", 90.0),
            t2_max_signals_per_cycle=_env_int("T2_MAX_SIGNALS_PER_CYCLE", 30),
            t2_updown_enabled=_env_bool("T2_UPDOWN_ENABLED", False),
            t2_updown_priority_boost=_env_float("T2_UPDOWN_PRIORITY_BOOST", 1.0),
            t2_updown_symbols=_env("T2_UPDOWN_SYMBOLS", "btc,eth"),
            t2_updown_window_minutes=_env("T2_UPDOWN_WINDOW_MINUTES", "15"),
            t2_updown_slots_ahead=_env_int("T2_UPDOWN_SLOTS_AHEAD", 4),
            t2_updown_max_spread_bps=_env_float("T2_UPDOWN_MAX_SPREAD_BPS", 500.0),
            t2_updown_spot_feed_enabled=_env_bool(
                "T2_UPDOWN_SPOT_FEED_ENABLED",
                _env_bool("T2_UPDOWN_ENABLED", False),
            ),
            t2_updown_spot_pairs=_env("T2_UPDOWN_SPOT_PAIRS", ""),
            t2_reject_price_below=_env_float("T2_REJECT_PRICE_BELOW", 0.10),
            t2_reject_price_above=_env_float("T2_REJECT_PRICE_ABOVE", 0.90),
            t2_near_efficient_min_net_edge_bps=_env_float(
                "T2_NEAR_EFFICIENT_MIN_NET_EDGE_BPS", 300.0
            ),
            t2_near_certainty_shadow_mode=_env_bool(
                "T2_NEAR_CERTAINTY_SHADOW_MODE", True
            ),
            t2_near_certainty_high_threshold=_env_float(
                "T2_NEAR_CERTAINTY_HIGH_THRESHOLD", 0.92
            ),
            t2_near_certainty_low_threshold=_env_float(
                "T2_NEAR_CERTAINTY_LOW_THRESHOLD", 0.08
            ),
            t2_near_certainty_size_multiplier=_env_float(
                "T2_NEAR_CERTAINTY_SIZE_MULTIPLIER", 0.60
            ),
            t2_near_certainty_confidence_delta=_env_float(
                "T2_NEAR_CERTAINTY_CONFIDENCE_DELTA", -0.08
            ),
            t2_barbell_enabled=_env_bool("T2_BARBELL_ENABLED", False),
            t2_barbell_tail_budget_pct=_env_float("T2_BARBELL_TAIL_BUDGET_PCT", 0.15),
            t2_barbell_tail_relaxed_multiplier=_env_float(
                "T2_BARBELL_TAIL_RELAXED_MULTIPLIER", 0.85
            ),
            t2_post_exit_cooldown_sec=_env_float("T2_POST_EXIT_COOLDOWN_SEC", 24 * 3600.0),
            t2_recent_exits_state_file=_env(
                "T2_RECENT_EXITS_STATE_FILE",
                "data/telemetry/recent_exits.json",
            ),
            t3_flow_bias_enabled=_env_bool("T3_FLOW_BIAS_ENABLED", True),
            t3_flow_bias_window_sec=_env_float("T3_FLOW_BIAS_WINDOW_SEC", 3600.0),
            t3_flow_bias_min_trades=_env_int("T3_FLOW_BIAS_MIN_TRADES", 20),
            t3_flow_bias_strong_threshold=_env_float(
                "T3_FLOW_BIAS_STRONG_THRESHOLD", 0.55
            ),
            t3_flow_bias_inventory_weight=_env_float(
                "T3_FLOW_BIAS_INVENTORY_WEIGHT", 0.5
            ),
            shadow_maker_fill_latency_sec=_env_float(
                "SHADOW_MAKER_FILL_LATENCY_SEC", 2.0
            ),
            t3_flow_state_file=_env(
                "T3_FLOW_STATE_FILE",
                "data/telemetry/flow_state.json",
            ),
            maker_max_hold_sec=_env_float("MAKER_MAX_HOLD_SEC", 6 * 3600.0),
            maker_stop_loss_bps=_env_float("MAKER_STOP_LOSS_BPS", 300.0),
            maker_take_profit_bps=_env_float("MAKER_TAKE_PROFIT_BPS", 200.0),
            maker_exit_eval_interval_sec=_env_float(
                "MAKER_EXIT_EVAL_INTERVAL_SEC", 30.0
            ),
            maker_rewards_enabled=_env_bool("MAKER_REWARDS_ENABLED", True),
            maker_rewards_ttl_sec=_env_float("MAKER_REWARDS_TTL_SEC", 900.0),
            maker_rewards_negative_ttl_sec=_env_float(
                "MAKER_REWARDS_NEGATIVE_TTL_SEC", 300.0
            ),
            maker_rewards_timeout_sec=_env_float("MAKER_REWARDS_TIMEOUT_SEC", 5.0),
            maker_rewards_prefetch_per_cycle=_env_int(
                "MAKER_REWARDS_PREFETCH_PER_CYCLE", 20
            ),
            maker_rewards_only=_env_bool("MAKER_REWARDS_ONLY", False),
            maker_scoring_audit_enabled=_env_bool("MAKER_SCORING_AUDIT_ENABLED", True),
            maker_scoring_audit_interval_sec=_env_float(
                "MAKER_SCORING_AUDIT_INTERVAL_SEC", 60.0
            ),
            maker_scoring_cancel_unscored=_env_bool(
                "MAKER_SCORING_CANCEL_UNSCORED", False
            ),
            maker_scoring_unscored_grace_sec=_env_float(
                "MAKER_SCORING_UNSCORED_GRACE_SEC", 90.0
            ),
            user_ws_enabled=_env_bool("USER_WS_ENABLED", True),
            user_ws_queue_size=_env_int("USER_WS_QUEUE_SIZE", 2000),
            user_ws_max_events_per_cycle=_env_int("USER_WS_MAX_EVENTS_PER_CYCLE", 500),
            t2_updown_rtds_mode=_env("T2_UPDOWN_RTDS_MODE", "shadow").lower() or "shadow",
            t2_updown_rtds_staleness_sec=_env_float("T2_UPDOWN_RTDS_STALENESS_SEC", 30.0),
            t2_updown_rtds_chainlink=_env_bool("T2_UPDOWN_RTDS_CHAINLINK", False),
            t2_updown_basis_log_interval_sec=_env_float(
                "T2_UPDOWN_BASIS_LOG_INTERVAL_SEC", 300.0
            ),
            t2_updown_basis_alert_bps=_env_float("T2_UPDOWN_BASIS_ALERT_BPS", 50.0),
            cross_platform_entity_veto_enabled=_env_bool(
                "CROSS_PLATFORM_ENTITY_VETO_ENABLED", True
            ),
            cross_platform_min_token_overlap=_env_float(
                "CROSS_PLATFORM_MIN_TOKEN_OVERLAP", 0.0
            ),
            t3_anti_snipe_enabled=_env_bool("T3_ANTI_SNIPE_ENABLED", True),
            t3_anti_snipe_jump_ticks=_env_float("T3_ANTI_SNIPE_JUMP_TICKS", 3.0),
            t3_anti_snipe_jump_pause_sec=_env_float(
                "T3_ANTI_SNIPE_JUMP_PAUSE_SEC", 20.0
            ),
            t3_anti_snipe_stable_ticks_required=_env_int(
                "T3_ANTI_SNIPE_STABLE_TICKS_REQUIRED", 2
            ),
            t3_anti_snipe_stable_band_ticks=_env_float(
                "T3_ANTI_SNIPE_STABLE_BAND_TICKS", 1.0
            ),
            t3_anti_snipe_ema_alpha=_env_float("T3_ANTI_SNIPE_EMA_ALPHA", 0.3),
            t3_anti_snipe_mid_history=_env_int("T3_ANTI_SNIPE_MID_HISTORY", 7),
            t3_anti_snipe_post_fill_cooldown_sec=_env_float(
                "T3_ANTI_SNIPE_POST_FILL_COOLDOWN_SEC", 15.0
            ),
            t3_anti_snipe_max_chase_ticks=_env_float(
                "T3_ANTI_SNIPE_MAX_CHASE_TICKS", 2.0
            ),
            tick_record_enabled=_env_bool("TICK_RECORD_ENABLED", False),
            tick_record_dir=_env("TICK_RECORD_DIR", "data/ticks"),
            telemetry_record_enabled=_env_bool("TELEMETRY_RECORD_ENABLED", False),
            telemetry_record_dir=_env("TELEMETRY_RECORD_DIR", "data/telemetry"),
            telemetry_async_write=_env_bool("TELEMETRY_ASYNC_WRITE", True),
            telemetry_async_queue_size=_env_int("TELEMETRY_ASYNC_QUEUE_SIZE", 10000),
            data_cleanup_enabled=_env_bool("DATA_CLEANUP_ENABLED", True),
            data_cleanup_interval_sec=_env_float("DATA_CLEANUP_INTERVAL_SEC", 3600.0),
            data_ticks_retention_days=_env_int("DATA_TICKS_RETENTION_DAYS", 7),
            data_ticks_max_gb=_env_float("DATA_TICKS_MAX_GB", 5.0),
            data_telemetry_retention_days=_env_int("DATA_TELEMETRY_RETENTION_DAYS", 14),
            data_telemetry_max_gb=_env_float("DATA_TELEMETRY_MAX_GB", 2.0),
            data_research_cache_retention_days=_env_int("DATA_RESEARCH_CACHE_RETENTION_DAYS", 14),
            data_research_cache_max_gb=_env_float("DATA_RESEARCH_CACHE_MAX_GB", 1.0),
            data_backtest_retention_days=_env_int("DATA_BACKTEST_RETENTION_DAYS", 30),
            data_backtest_max_gb=_env_float("DATA_BACKTEST_MAX_GB", 2.0),
            dashboard_enabled=_env_bool("DASHBOARD_ENABLED", False),
            dashboard_port=_env_int("DASHBOARD_PORT", 8077),
            log_level=_env("LOG_LEVEL", "INFO"),
            log_file=_env("LOG_FILE", "arb_bot.log"),
            ws_enabled=_env_bool("WS_ENABLED", True),
            # Default sized to comfortably cover ARB_HOT_MARKET_POOL_SIZE
            # without forcing every cycle's snapshots through REST. The
            # canary template still overrides to 10. Going from 3 → 20
            # closes the gap that pushed live WS hit-ratio below 50%.
            ws_max_markets=_env_int("WS_MAX_MARKETS", 80),
            ws_refresh_cycles=_env_int("WS_REFRESH_CYCLES", 200),
            ws_vol_feed_interval_sec=_env_float("WS_VOL_FEED_INTERVAL_SEC", 60.0),
            ai_provider=_env("AI_PROVIDER", "openai"),
            ai_api_key=_env("AI_API_KEY") or _env("OPENAI_API_KEY"),
            ai_api_base=_env("AI_API_BASE"),
            ai_model=_env("AI_MODEL", "gpt-4o"),
            ai_temperature=_env_float("AI_TEMPERATURE", 0.1),
            research_signal_enabled=_env_bool("RESEARCH_SIGNAL_ENABLED", False),
            research_signal_window_sec=_env_int("RESEARCH_SIGNAL_WINDOW_SEC", 86400),
            research_signal_max_items=_env_int("RESEARCH_SIGNAL_MAX_ITEMS", 5),
            research_signal_cache_ttl_sec=_env_int("RESEARCH_SIGNAL_CACHE_TTL_SEC", 300),
            research_signal_cache_dir=_env("RESEARCH_SIGNAL_CACHE_DIR", "data/research_signal"),
            research_signal_http_json_sources=_env("RESEARCH_SIGNAL_HTTP_JSON_SOURCES", ""),
            research_signal_feeds_file=_env("RESEARCH_SIGNAL_FEEDS_FILE", "data/quant_inputs/research_feeds.json"),
            research_signal_crypto_macro_enabled=_env_bool(
                "RESEARCH_SIGNAL_CRYPTO_MACRO_ENABLED", False
            ),
            research_signal_coingecko_enabled=_env_bool(
                "RESEARCH_SIGNAL_COINGECKO_ENABLED", True
            ),
            research_signal_funding_rate_enabled=_env_bool(
                "RESEARCH_SIGNAL_FUNDING_RATE_ENABLED", True
            ),
            research_signal_econ_calendar_enabled=_env_bool(
                "RESEARCH_SIGNAL_ECON_CALENDAR_ENABLED", True
            ),
            research_signal_defillama_enabled=_env_bool(
                "RESEARCH_SIGNAL_DEFILLAMA_ENABLED", True
            ),
            research_signal_polymarket_activity_enabled=_env_bool(
                "RESEARCH_SIGNAL_POLYMARKET_ACTIVITY_ENABLED", True
            ),
            research_signal_manifold_enabled=_env_bool(
                "RESEARCH_SIGNAL_MANIFOLD_ENABLED", True
            ),
            backtest_enabled=_env_bool("BACKTEST_ENABLED", False),
            backtest_data_dir=_env("BACKTEST_DATA_DIR", "data/backtest"),
            backtest_default_dataset=_env("BACKTEST_DEFAULT_DATASET", "default"),
            backtest_slippage_bps=_env_float("BACKTEST_SLIPPAGE_BPS", 5.0),
            backtest_reports_dir=_env("BACKTEST_REPORTS_DIR", "research/backtest/output"),
            sniper_gate_enabled=_env_bool("SNIPER_GATE_ENABLED", False),
            sniper_min_net_edge_bps=_env_float("SNIPER_MIN_NET_EDGE_BPS", 500.0),
            sniper_min_confidence=_env_float("SNIPER_MIN_CONFIDENCE", 0.75),
            sniper_min_liquidity=_env_float("SNIPER_MIN_LIQUIDITY", 0.0),
            sniper_min_volume_24h=_env_float("SNIPER_MIN_VOLUME_24H", 0.0),
            sniper_max_correlation_score=_env_float("SNIPER_MAX_CORRELATION_SCORE", 0.80),
            logical_constraints_json=_env("LOGICAL_CONSTRAINTS_JSON", ""),
            event_baselines_json=_env("EVENT_BASELINES_JSON", ""),
            wallet_alpha_profiles_json=_env("WALLET_ALPHA_PROFILES_JSON", ""),
            wallet_alpha_observations_json=_env("WALLET_ALPHA_OBSERVATIONS_JSON", ""),
            logical_constraints_file=_env("LOGICAL_CONSTRAINTS_FILE", ""),
            event_baselines_file=_env("EVENT_BASELINES_FILE", ""),
            wallet_alpha_profiles_file=_env("WALLET_ALPHA_PROFILES_FILE", ""),
            wallet_alpha_observations_file=_env("WALLET_ALPHA_OBSERVATIONS_FILE", ""),
            wallet_alpha_candidate_shadow_enabled=_env_bool("WALLET_ALPHA_CANDIDATE_SHADOW_ENABLED", False),
            wallet_alpha_shadow_validation_enabled=_env_bool("WALLET_ALPHA_SHADOW_VALIDATION_ENABLED", False),
            wallet_alpha_shadow_max_signals_per_cycle=_env_int(
                "WALLET_ALPHA_SHADOW_MAX_SIGNALS_PER_CYCLE",
                5,
            ),
            wallet_alpha_shadow_max_exec_ms_per_cycle=_env_float(
                "WALLET_ALPHA_SHADOW_MAX_EXEC_MS_PER_CYCLE",
                250.0,
            ),
            weather_strategy_enabled=_env_bool("WEATHER_STRATEGY_ENABLED", False),
            weather_min_edge=_env_float("WEATHER_MIN_EDGE", 0.10),
            weather_min_confidence=_env_float("WEATHER_MIN_CONFIDENCE", 0.70),
            weather_max_spread_bps=_env_float("WEATHER_MAX_SPREAD_BPS", 180.0),
            weather_min_top_depth=_env_float("WEATHER_MIN_TOP_DEPTH", 25.0),
            weather_forecast_ttl_sec=_env_float("WEATHER_FORECAST_TTL_SEC", 900.0),
            weather_request_timeout_sec=_env_float("WEATHER_REQUEST_TIMEOUT_SEC", 10.0),
            weather_max_markets=_env_int("WEATHER_MAX_MARKETS", 40),
        )

        LOG.info(
            "配置加载完成: dry_run=%s, min_edge=%.4f USD / %.2f%%, scan_interval=%.1fs",
            cfg.dry_run,
            cfg.min_edge_usd,
            cfg.min_edge_pct,
            cfg.scan_interval_sec,
        )
        return cfg

    def dump_safe(self) -> dict:
        """输出不含敏感信息的配置摘要."""
        d = {}
        for k, v in self.__dict__.items():
            key_lower = k.lower()
            if "key" in key_lower or "token" in key_lower or "secret" in key_lower or "webhook" in key_lower:
                d[k] = "***"
            elif k == "funder_address" and isinstance(v, str) and v:
                d[k] = _mask_sensitive_value(v)
            else:
                d[k] = v
        return d


def _mask_sensitive_value(value: str, *, prefix: int = 6, suffix: int = 4) -> str:
    if not value:
        return ""
    if len(value) <= prefix + suffix:
        return "*" * len(value)
    return f"{value[:prefix]}***{value[-suffix:]}"
