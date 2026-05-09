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
