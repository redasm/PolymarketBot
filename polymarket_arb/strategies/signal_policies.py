"""Signal-overlay policies (tail risk, research veto, etc).

Extracted from `StrategyOrchestrator` so the orchestrator stays focused on
priority scheduling and capital allocation. Each policy is data-driven via a
small set of dataclasses, so keyword lists can evolve (or be loaded from
config) without touching scheduler logic.

Why this is a separate module:
- `_classify_tail_risk` previously hard-coded multi-line keyword tuples
  inside the orchestrator; that is exactly the kind of policy data that
  needs frequent tuning (election cycles, new geopolitical hotspots, sport
  seasons) and should not require touching scheduling code.
- Future overlays (e.g. weather/news veto, regulatory blackouts) can plug
  in by appending a new TailRiskRule or sibling policy without growing the
  orchestrator class further.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Sequence


@dataclass(frozen=True)
class TailRiskRule:
    """A single matcher: text contains any keyword -> apply this rule."""

    risk_class: str
    keywords: tuple[str, ...]
    size_multiplier: float = 1.0
    confidence_delta: float = 0.0
    reasons: tuple[str, ...] = ()


# Order matters: the first rule whose keywords hit wins. High-tail must come
# first so a "ceasefire election" headline still trips the geopolitical
# discount instead of being swallowed by the medium-tail "election" bucket.
DEFAULT_TAIL_RISK_RULES: tuple[TailRiskRule, ...] = (
    TailRiskRule(
        risk_class="high_tail",
        keywords=(
            "war", "ceasefire", "missile", "invasion", "iran", "israel", "russia", "ukraine",
            "china", "taiwan", "geopolit", "hostage", "terror", "coup", "nuclear", "assassination",
            "supreme court", "resign", "death", "fired", "will trump", "will biden",
        ),
        size_multiplier=0.50,
        confidence_delta=-0.10,
        reasons=("tail_risk_high", "kelly_fraction_discount"),
    ),
    TailRiskRule(
        risk_class="medium_tail",
        keywords=(
            "election", "president", "nominee", "crypto", "bitcoin", "btc", "ethereum", "eth",
            "solana", "fed", "fomc", "rate cut", "cpi", "inflation", "sec", "lawsuit",
        ),
        reasons=("tail_risk_medium",),
    ),
    TailRiskRule(
        risk_class="data_driven",
        keywords=(
            "economic data", "jobless", "payroll", "unemployment", "gdp", "pce", "cpi",
            "sports", "nba", "nfl", "mlb", "nhl", "weather",
        ),
        reasons=("tail_risk_low",),
    ),
)

UNKNOWN_TAIL_RISK = TailRiskRule(
    risk_class="unknown",
    keywords=(),
    reasons=("tail_risk_unknown",),
)


# ---------------------------------------------------------------------------
# Near-certainty rule
# ---------------------------------------------------------------------------
#
# Article 4 (Taleb / @stacyonchain): markets priced 92-98¢ systematically
# underprice tail risk. Two anecdotes ⇒ not statistics. Empirical
# verification (scripts/verify_near_certainty_trap.py) was BLOCKED on
# free-tier data availability (CLOB drops resolved-market history;
# Goldsky subgraph times out on heavy markets). Decision: ship the rule
# in SHADOW MODE — compute and log what it WOULD have done on every
# directional signal, but do not modify the signal in production until
# we accumulate enough live samples to validate the size discount.
#
# When shadow_mode is off, the rule applies a size_multiplier and
# confidence_delta similar to TailRiskRule. The rule fires on both
# symmetric tails: market_price ≥ high_threshold (buying the
# near-certain side at near 1.0) AND market_price ≤ low_threshold
# (buying the long-shot side at near 0.0). The longshot side is
# already covered by `t2_reject_price_below`, but the high-side gate
# exists only here.


# ---------------------------------------------------------------------------
# Barbell pool policy
# ---------------------------------------------------------------------------
#
# Article 4 / Taleb: split capital into a large "data-driven" bucket
# (Mediocristan markets, small reliable EV) and a smaller "tail" bucket
# (Extremistan markets, asymmetric convex bets). The bot's existing
# tail_risk discount (0.5×) is appropriate when there's no separate
# tail bucket — it prevents over-allocation to geopolitical-style
# markets. But once tail bets are sized out of a dedicated bucket
# capped at e.g. 15% of T2, the per-bet discount can be relaxed
# because portfolio-level concentration is already controlled.
#
# `BarbellPolicy` is a pure decision function: given current tail
# exposure + budget cap, return what size multiplier should override
# the tail_risk rule's default. It is *not* an exposure tracker; the
# orchestrator owns that state (see _t2_class_exposure_usdc).


@dataclass(frozen=True)
class BarbellDecision:
    bucket: str  # "data_driven" | "tail" | "reserve" | "tail_full"
    multiplier_override: float | None  # None => use rule default
    reasons: list[str] = field(default_factory=list)


class BarbellPolicy:
    def __init__(
        self,
        *,
        enabled: bool,
        tail_budget_usdc: float,
        tail_relaxed_multiplier: float = 0.85,
    ) -> None:
        if tail_budget_usdc < 0:
            raise ValueError("tail_budget_usdc must be >= 0")
        if not (0.0 < tail_relaxed_multiplier <= 1.0):
            raise ValueError("tail_relaxed_multiplier must be in (0, 1]")
        self._enabled = bool(enabled)
        self._tail_budget = float(tail_budget_usdc)
        self._tail_relax = float(tail_relaxed_multiplier)

    @property
    def enabled(self) -> bool:
        return self._enabled

    @property
    def tail_budget_usdc(self) -> float:
        return self._tail_budget

    def classify_bucket(self, risk_class: str) -> str:
        if not risk_class:
            return "data_driven"
        if risk_class == "high_tail":
            return "tail"
        return "data_driven"

    def decide(
        self,
        *,
        risk_class: str,
        signal_size_usdc: float,
        current_tail_exposure_usdc: float,
    ) -> BarbellDecision:
        bucket = self.classify_bucket(risk_class)
        if not self._enabled:
            return BarbellDecision(
                bucket=bucket,
                multiplier_override=None,
                reasons=["barbell_disabled"],
            )
        if bucket != "tail":
            return BarbellDecision(
                bucket=bucket,
                multiplier_override=None,
                reasons=["barbell_data_driven_routing"],
            )
        # Tail signal under barbell — does the tail bucket have room?
        projected = current_tail_exposure_usdc + max(0.0, float(signal_size_usdc))
        if projected <= self._tail_budget:
            return BarbellDecision(
                bucket="tail",
                multiplier_override=self._tail_relax,
                reasons=["barbell_tail_bucket_has_room", f"projected<={self._tail_budget:.2f}"],
            )
        return BarbellDecision(
            bucket="tail_full",
            multiplier_override=None,
            reasons=["barbell_tail_bucket_full", f"projected>{self._tail_budget:.2f}"],
        )


@dataclass(frozen=True)
class NearCertaintyResult:
    applied: bool
    shadow_mode: bool
    market_price: float | None
    risk_zone: str  # "high_certainty" | "longshot" | "normal" | "unknown"
    size_multiplier: float
    confidence_delta: float
    reasons: list[str] = field(default_factory=list)


class NearCertaintyClassifier:
    """Apply size/confidence discount when entering near a binary boundary.

    Parameters
    ----------
    high_threshold : market price at/above which a BUY YES (or any buy
        of the on-side outcome) trips the rule. Default 0.92.
    low_threshold : symmetric on the long-shot tail. Default 0.08.
    size_multiplier : the size discount applied when the rule fires
        (and shadow_mode is off). Default 0.60.
    confidence_delta : confidence adjustment when the rule fires.
        Default -0.08.
    shadow_mode : when True, the classifier still computes what it
        would have done but the caller treats the discount as
        informational-only. Default True.
    """

    def __init__(
        self,
        *,
        high_threshold: float = 0.92,
        low_threshold: float = 0.08,
        size_multiplier: float = 0.60,
        confidence_delta: float = -0.08,
        shadow_mode: bool = True,
    ) -> None:
        if not (0.5 < high_threshold <= 1.0):
            raise ValueError("high_threshold must be in (0.5, 1.0]")
        if not (0.0 <= low_threshold < 0.5):
            raise ValueError("low_threshold must be in [0.0, 0.5)")
        if not (0.0 < size_multiplier <= 1.0):
            raise ValueError("size_multiplier must be in (0.0, 1.0]")
        self._high = float(high_threshold)
        self._low = float(low_threshold)
        self._size_mult = float(size_multiplier)
        self._conf_delta = float(confidence_delta)
        self._shadow = bool(shadow_mode)

    @property
    def shadow_mode(self) -> bool:
        return self._shadow

    def classify(self, market_price: float | None) -> NearCertaintyResult:
        if market_price is None:
            return NearCertaintyResult(
                applied=False,
                shadow_mode=self._shadow,
                market_price=None,
                risk_zone="unknown",
                size_multiplier=1.0,
                confidence_delta=0.0,
                reasons=["near_certainty_no_price"],
            )
        price = float(market_price)
        if price >= self._high:
            return NearCertaintyResult(
                applied=True,
                shadow_mode=self._shadow,
                market_price=price,
                risk_zone="high_certainty",
                size_multiplier=self._size_mult,
                confidence_delta=self._conf_delta,
                reasons=[
                    "near_certainty_high_zone",
                    f"market_price>={self._high:.2f}",
                ],
            )
        if price <= self._low:
            return NearCertaintyResult(
                applied=True,
                shadow_mode=self._shadow,
                market_price=price,
                risk_zone="longshot",
                size_multiplier=self._size_mult,
                confidence_delta=self._conf_delta,
                reasons=[
                    "near_certainty_longshot_zone",
                    f"market_price<={self._low:.2f}",
                ],
            )
        return NearCertaintyResult(
            applied=False,
            shadow_mode=self._shadow,
            market_price=price,
            risk_zone="normal",
            size_multiplier=1.0,
            confidence_delta=0.0,
            reasons=[],
        )


@dataclass(frozen=True)
class TailRiskClassification:
    risk_class: str
    size_multiplier: float
    confidence_delta: float
    reasons: list[str] = field(default_factory=list)


class TailRiskClassifier:
    """Classify market text against an ordered list of tail-risk rules."""

    def __init__(self, rules: Sequence[TailRiskRule] | None = None) -> None:
        self._rules: tuple[TailRiskRule, ...] = tuple(rules) if rules else DEFAULT_TAIL_RISK_RULES

    @property
    def rules(self) -> tuple[TailRiskRule, ...]:
        return self._rules

    def classify(self, text: str) -> TailRiskClassification:
        """Return the first matching rule, or `unknown` when no keywords hit."""
        haystack = (text or "").lower()
        for rule in self._rules:
            if any(keyword in haystack for keyword in rule.keywords):
                return TailRiskClassification(
                    risk_class=rule.risk_class,
                    size_multiplier=rule.size_multiplier,
                    confidence_delta=rule.confidence_delta,
                    reasons=list(rule.reasons),
                )
        return TailRiskClassification(
            risk_class=UNKNOWN_TAIL_RISK.risk_class,
            size_multiplier=UNKNOWN_TAIL_RISK.size_multiplier,
            confidence_delta=UNKNOWN_TAIL_RISK.confidence_delta,
            reasons=list(UNKNOWN_TAIL_RISK.reasons),
        )
