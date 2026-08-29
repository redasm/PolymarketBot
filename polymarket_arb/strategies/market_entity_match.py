"""跨平台配对的实体一致性校验（只否决，不建对）.

T1 的配对来自手写的 `CROSS_PLATFORM_PAIRS_JSON`。手写配对最危险的失败
模式不是"配漏了"，而是**配错了**：两个平台的问题看起来是同一件事，但
阈值、日期或方向不同 —— 于是 `poly_yes + kalshi_no < 1` 看起来像无风险
套利，实际上两条腿可能同时输，因为它们根本不是互补的同一事件。

举几个会被这里拦下的例子:

  - "BTC above $100k on Dec 31" vs "BTC above $100k on Jan 31" → 日期不同
  - "BTC above $100k"           vs "BTC above $120k"           → 阈值不同
  - "BTC above $100k"           vs "BTC below $100k"           → 方向相反
  - "Lakers win the title"      vs "Celtics win the title"     → 主体不同

设计取舍:

1. **只否决，不建对**。本模块永远不会把两个市场判定为"同一事件"，只会
   在证据明确矛盾时说"这两个肯定不是同一事件"。自动配对需要语义模型，
   误配的代价远高于漏配，不在这一步做。
2. **缺信息 = 不否决**。一侧没抽到实体就放行 —— 宁可放过一个错配，也
   不能因为文案措辞差异静默关掉用户手写的合法配对。
3. **只比双方都抽到的类型**。一边提到年份、另一边没提，不算矛盾。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Iterable

# 只在双方都抽到该类型时才比较；任一侧为空一律放行。
CRITICAL_ENTITY_TYPES = ("thresholds", "dates", "years")

_STOP_WORDS = frozenset(
    """
    a an the will be is are was were do does did to of in on at by for with
    and or if then than that this these those it its as from any all
    market markets question resolve resolves resolved yes no
    """.split()
)

_MONTHS = {
    "jan": 1, "january": 1, "feb": 2, "february": 2, "mar": 3, "march": 3,
    "apr": 4, "april": 4, "may": 5, "jun": 6, "june": 6, "jul": 7, "july": 7,
    "aug": 8, "august": 8, "sep": 9, "sept": 9, "september": 9, "oct": 10,
    "october": 10, "nov": 11, "november": 11, "dec": 12, "december": 12,
}

_ABOVE_TERMS = (
    "above", "over", "higher", "greater", "at least", "more than", "exceed",
    "exceeds", "up", "rise", "rises", "gain", "gains", "beat", "beats", ">=", ">",
)
_BELOW_TERMS = (
    "below", "under", "lower", "less than", "at most", "fewer", "down", "fall",
    "falls", "drop", "drops", "decline", "declines", "<=", "<",
)

_MULTIPLIERS = {"k": 1e3, "m": 1e6, "b": 1e9, "bn": 1e9, "t": 1e12}

_ISO_DATE_RE = re.compile(r"\b(20\d{2})-(\d{1,2})-(\d{1,2})\b")
_MONTH_DAY_RE = re.compile(
    r"\b(" + "|".join(sorted(_MONTHS, key=len, reverse=True)) + r")\.?\s+(\d{1,2})(?:st|nd|rd|th)?\b"
)
_DAY_MONTH_RE = re.compile(
    r"\b(\d{1,2})(?:st|nd|rd|th)?\s+(" + "|".join(sorted(_MONTHS, key=len, reverse=True)) + r")\b"
)
_YEAR_RE = re.compile(r"\b(20\d{2})\b")
_MONEY_RE = re.compile(r"\$\s*([\d,]+(?:\.\d+)?)\s*([kmbt]|bn)?\b", re.IGNORECASE)
_PERCENT_RE = re.compile(r"\b([\d,]+(?:\.\d+)?)\s*(?:%|percent|pct)(?![a-z])", re.IGNORECASE)
_BPS_RE = re.compile(r"\b([\d,]+(?:\.\d+)?)\s*(?:bps|basis\s+points?)\b", re.IGNORECASE)
_BARE_NUM_RE = re.compile(r"\b([\d,]+(?:\.\d+)?)\s*([kmbt]|bn)\b", re.IGNORECASE)


@dataclass(frozen=True)
class ExtractedEntities:
    thresholds: frozenset[str] = field(default_factory=frozenset)
    dates: frozenset[str] = field(default_factory=frozenset)
    years: frozenset[str] = field(default_factory=frozenset)
    direction: str = ""  # "above" / "below" / "" (未检出或两者皆有)
    tokens: frozenset[str] = field(default_factory=frozenset)

    def to_dict(self) -> dict:
        return {
            "thresholds": sorted(self.thresholds),
            "dates": sorted(self.dates),
            "years": sorted(self.years),
            "direction": self.direction,
        }


@dataclass(frozen=True)
class MatchVerification:
    ok: bool
    mismatches: tuple[str, ...] = field(default_factory=tuple)
    token_overlap: float = 0.0
    left: ExtractedEntities = field(default_factory=ExtractedEntities)
    right: ExtractedEntities = field(default_factory=ExtractedEntities)

    def to_dict(self) -> dict:
        return {
            "ok": self.ok,
            "mismatches": list(self.mismatches),
            "token_overlap": round(self.token_overlap, 4),
            "left": self.left.to_dict(),
            "right": self.right.to_dict(),
        }


def _clean_number(raw: str) -> str:
    """把 "1,250.00" 规整成 "1250"，让 1,250 和 1250.0 判为同一个数."""
    text = raw.replace(",", "").strip()
    try:
        value = float(text)
    except ValueError:
        return text
    if value == int(value):
        return str(int(value))
    return repr(value)


def _scaled(raw: str, suffix: str | None) -> str:
    value = _clean_number(raw)
    if not suffix:
        return value
    factor = _MULTIPLIERS.get(suffix.lower())
    if factor is None:
        return value
    try:
        return _clean_number(str(float(value) * factor))
    except ValueError:
        return value


def normalize_text(text: str) -> str:
    return re.sub(r"\s+", " ", str(text or "").lower()).strip()


def tokenize(text: str) -> frozenset[str]:
    words = re.findall(r"[a-z0-9$%.]+", normalize_text(text))
    return frozenset(w for w in words if len(w) > 1 and w not in _STOP_WORDS)


def _extract_direction(text: str) -> str:
    has_above = any(term in text for term in _ABOVE_TERMS)
    has_below = any(term in text for term in _BELOW_TERMS)
    if has_above and not has_below:
        return "above"
    if has_below and not has_above:
        return "below"
    # 两者都出现（"above X but below Y"）时不下结论，避免误否决。
    return ""


def extract_entities(text: str) -> ExtractedEntities:
    """抽出方向、阈值、日期、年份。抽不到就是空集，不猜."""
    norm = normalize_text(text)

    thresholds: set[str] = set()
    for raw, suffix in _MONEY_RE.findall(norm):
        thresholds.add("$" + _scaled(raw, suffix))
    for raw in _PERCENT_RE.findall(norm):
        thresholds.add(_clean_number(raw) + "%")
    for raw in _BPS_RE.findall(norm):
        thresholds.add(_clean_number(raw) + "bp")
    for raw, suffix in _BARE_NUM_RE.findall(norm):
        thresholds.add(_scaled(raw, suffix))

    dates: set[str] = set()
    for year, month, day in _ISO_DATE_RE.findall(norm):
        dates.add(f"{int(month):02d}-{int(day):02d}")
    for month, day in _MONTH_DAY_RE.findall(norm):
        dates.add(f"{_MONTHS[month]:02d}-{int(day):02d}")
    for day, month in _DAY_MONTH_RE.findall(norm):
        dates.add(f"{_MONTHS[month]:02d}-{int(day):02d}")

    years = {y for y in _YEAR_RE.findall(norm)}

    return ExtractedEntities(
        thresholds=frozenset(thresholds),
        dates=frozenset(dates),
        years=frozenset(years),
        direction=_extract_direction(norm),
        tokens=tokenize(norm),
    )


def _jaccard(left: Iterable[str], right: Iterable[str]) -> float:
    a, b = set(left), set(right)
    if not a or not b:
        return 0.0
    return len(a & b) / len(a | b)


def verify_pair_match(
    left_text: str,
    right_text: str,
    *,
    min_token_overlap: float = 0.0,
) -> MatchVerification:
    """校验两侧问题描述是否可能指同一事件.

    `ok=False` 意味着**证据表明它们不是同一事件**，不是"不确定"。任一
    侧文本为空一律放行（`ok=True`）—— 缺信息不构成矛盾。

    `min_token_overlap > 0` 时额外要求词元 Jaccard 重叠不低于该值。默认
    0（关闭）：措辞差异很大但确实同一事件的配对在跨平台里很常见，用词
    重叠去否决容易误伤。
    """
    if not str(left_text or "").strip() or not str(right_text or "").strip():
        return MatchVerification(ok=True)

    left = extract_entities(left_text)
    right = extract_entities(right_text)
    overlap = _jaccard(left.tokens, right.tokens)

    mismatches: list[str] = []
    for entity_type in CRITICAL_ENTITY_TYPES:
        left_set = getattr(left, entity_type)
        right_set = getattr(right, entity_type)
        # 只比双方都抽到的类型；一边没提不算矛盾。
        if left_set and right_set and not (left_set & right_set):
            mismatches.append(f"{entity_type}_mismatch")

    if left.direction and right.direction and left.direction != right.direction:
        # 方向相反是最隐蔽的错配：实体全都一样，含义正好相反。
        mismatches.append("direction_mismatch")

    if min_token_overlap > 0 and overlap < min_token_overlap:
        mismatches.append("low_token_overlap")

    return MatchVerification(
        ok=not mismatches,
        mismatches=tuple(mismatches),
        token_overlap=overlap,
        left=left,
        right=right,
    )
