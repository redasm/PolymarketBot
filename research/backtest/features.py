"""Feature extraction helpers for offline prediction-market research."""

from __future__ import annotations

from typing import Any


def summarize_binary_microstructure(row: dict[str, Any]) -> dict[str, float | None]:
    yes_bid = _as_float(row.get("yes_best_bid"))
    yes_ask = _as_float(row.get("yes_best_ask"))
    yes_bid_size = _as_float(row.get("yes_bid_size"))
    yes_ask_size = _as_float(row.get("yes_ask_size"))
    no_bid = _as_float(row.get("no_best_bid"))
    no_ask = _as_float(row.get("no_best_ask"))
    no_bid_size = _as_float(row.get("no_bid_size"))
    no_ask_size = _as_float(row.get("no_ask_size"))

    yes_mid = _mid(yes_bid, yes_ask)
    no_mid = _mid(no_bid, no_ask)
    return {
        "yes_mid": yes_mid,
        "no_mid": no_mid,
        "yes_spread_bps": _spread_bps(yes_bid, yes_ask),
        "no_spread_bps": _spread_bps(no_bid, no_ask),
        "yes_imbalance": _imbalance(yes_bid_size, yes_ask_size),
        "no_imbalance": _imbalance(no_bid_size, no_ask_size),
        "microprice_yes": _microprice(yes_bid, yes_ask, yes_bid_size, yes_ask_size),
        "microprice_no": _microprice(no_bid, no_ask, no_bid_size, no_ask_size),
        "sum_best_asks": _safe_add(yes_ask, no_ask),
        "sum_mids": _safe_add(yes_mid, no_mid),
        "complement_error_bps": _complement_error_bps(yes_mid, no_mid),
    }


def _as_float(value: Any) -> float | None:
    if value is None or value == "":
        return None
    return float(value)


def _mid(bid: float | None, ask: float | None) -> float | None:
    if bid is None or ask is None:
        return None
    return (bid + ask) / 2.0


def _spread_bps(bid: float | None, ask: float | None) -> float | None:
    mid = _mid(bid, ask)
    if mid is None or mid <= 0 or bid is None or ask is None:
        return None
    return ((ask - bid) / mid) * 10_000.0


def _imbalance(bid_size: float | None, ask_size: float | None) -> float | None:
    if bid_size is None or ask_size is None:
        return None
    total = bid_size + ask_size
    if total <= 0:
        return 0.0
    return (bid_size - ask_size) / total


def _microprice(
    bid: float | None,
    ask: float | None,
    bid_size: float | None,
    ask_size: float | None,
) -> float | None:
    if None in {bid, ask, bid_size, ask_size}:
        return None
    total = float(bid_size) + float(ask_size)
    if total <= 0:
        return None
    return ((float(ask) * float(bid_size)) + (float(bid) * float(ask_size))) / total


def _safe_add(left: float | None, right: float | None) -> float | None:
    if left is None or right is None:
        return None
    return left + right


def _complement_error_bps(yes_mid: float | None, no_mid: float | None) -> float | None:
    if yes_mid is None or no_mid is None:
        return None
    return abs(1.0 - (yes_mid + no_mid)) * 10_000.0
