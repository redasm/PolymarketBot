"""Helpers for building backtest datasets from recorded live artifacts."""

from __future__ import annotations

import json
from pathlib import Path


def build_binary_snapshots_from_ticks(
    ticks_path: str | Path,
    output_dir: str | Path,
    *,
    condition_id: str | None = None,
    event_id: str | None = None,
    question: str | None = None,
    slug: str | None = None,
    yes_token_id: str | None = None,
    no_token_id: str | None = None,
    condition_id_filter: str | None = None,
    strict: bool = True,
) -> Path:
    ticks_file = Path(ticks_path)
    out_dir = Path(output_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    output_path = out_dir / "market_snapshots.jsonl"

    rows = [json.loads(line) for line in ticks_file.read_text(encoding="utf-8-sig").splitlines() if line.strip()]
    if condition_id_filter:
        rows = [row for row in rows if str(row.get("condition_id") or "") == condition_id_filter]
    elif len(_distinct_condition_ids(rows)) > 1:
        raise ValueError("tick rows contain multiple condition_id values; pass condition_id_filter or use the batch dataset builder")
    rows.sort(key=lambda row: int(row.get("ts_ms", 0)))
    if rows:
        meta = _extract_market_metadata(rows[0])
        condition_id = condition_id or meta.get("condition_id") or "from_ticks"
        event_id = event_id or meta.get("event_id") or "from_ticks"
        question = question or meta.get("question") or "Recovered from ticks"
        slug = slug or meta.get("slug") or "recovered-from-ticks"
    else:
        condition_id = condition_id or "from_ticks"
        event_id = event_id or "from_ticks"
        question = question or "Recovered from ticks"
        slug = slug or "recovered-from-ticks"

    snapshots: list[dict] = []
    role_map = _resolve_role_map(rows, yes_token_id=yes_token_id, no_token_id=no_token_id, strict=strict)
    latest_state: dict[str, dict] = {}

    for row in rows:
        token_id = str(row.get("token_id") or "")
        if not token_id:
            continue
        role = role_map.get(token_id)
        if role is None:
            continue

        ts_ms = int(row.get("ts_ms", 0))
        latest_state[role] = {
            "token_id": token_id,
            "best_bid": row.get("best_bid"),
            "best_ask": row.get("best_ask"),
            "bid_size": _top_level_size(row.get("bids_top3")),
            "ask_size": _top_level_size(row.get("asks_top3")),
            "bid_levels": _levels_from_top(row.get("bids_top3")),
            "ask_levels": _levels_from_top(row.get("asks_top3")),
        }

        if _is_complete_latest_state(latest_state):
            snapshot = {
                "ts_ms": ts_ms,
                "condition_id": condition_id,
                "event_id": event_id,
                "question": question,
                "slug": slug,
                "yes_token_id": latest_state["yes"]["token_id"],
                "no_token_id": latest_state["no"]["token_id"],
                "yes_best_bid": latest_state["yes"]["best_bid"],
                "yes_best_ask": latest_state["yes"]["best_ask"],
                "yes_bid_size": latest_state["yes"]["bid_size"],
                "yes_ask_size": latest_state["yes"]["ask_size"],
                "yes_bid_levels": latest_state["yes"]["bid_levels"],
                "yes_ask_levels": latest_state["yes"]["ask_levels"],
                "no_best_bid": latest_state["no"]["best_bid"],
                "no_best_ask": latest_state["no"]["best_ask"],
                "no_bid_size": latest_state["no"]["bid_size"],
                "no_ask_size": latest_state["no"]["ask_size"],
                "no_bid_levels": latest_state["no"]["bid_levels"],
                "no_ask_levels": latest_state["no"]["ask_levels"],
                "best_ask": latest_state["yes"]["best_ask"],
                "available_size": latest_state["yes"]["ask_size"],
            }
            snapshots.append(snapshot)

    lines = [json.dumps(snapshot, ensure_ascii=False) for snapshot in snapshots]
    output_path.write_text("\n".join(lines) + ("\n" if lines else ""), encoding="utf-8")
    return output_path

def _is_complete_latest_state(state: dict[str, dict]) -> bool:
    return "yes" in state and "no" in state and all(
        state[role].get(key) is not None
        for role in ("yes", "no")
        for key in ("token_id", "best_bid", "best_ask")
    )


def _resolve_role_map(
    rows: list[dict],
    *,
    yes_token_id: str | None,
    no_token_id: str | None,
    strict: bool,
) -> dict[str, str]:
    if yes_token_id and no_token_id:
        return {str(yes_token_id): "yes", str(no_token_id): "no"}

    explicit_roles = {
        str(row.get("token_id") or ""): str(row.get("outcome_role") or "").lower()
        for row in rows
        if str(row.get("token_id") or "").strip() and str(row.get("outcome_role") or "").lower() in {"yes", "no"}
    }
    yes_from_role = next((token_id for token_id, role in explicit_roles.items() if role == "yes"), None)
    no_from_role = next((token_id for token_id, role in explicit_roles.items() if role == "no"), None)
    if yes_from_role and no_from_role and yes_from_role != no_from_role:
        return {yes_from_role: "yes", no_from_role: "no"}
    if strict:
        raise ValueError("tick rows missing explicit outcome_role metadata; rerun recording or pass strict=False")

    token_ids = sorted(
        {
            str(row.get("token_id") or "")
            for row in rows
            if str(row.get("token_id") or "").strip()
        }
    )
    if len(token_ids) < 2:
        return {}
    named_yes = next((token_id for token_id in token_ids if "yes" in token_id.lower()), None)
    named_no = next((token_id for token_id in token_ids if "no" in token_id.lower()), None)
    if named_yes and named_no and named_yes != named_no:
        return {named_yes: "yes", named_no: "no"}
    # Deterministic fallback keeps datasets reproducible, but explicit mapping is preferred.
    return {token_ids[0]: "yes", token_ids[1]: "no"}


def _top_level_size(levels: object) -> float:
    parsed = _levels_from_top(levels)
    return float(parsed[0][1]) if parsed else 0.0


def _levels_from_top(levels: object) -> list[list[float]]:
    parsed: list[list[float]] = []
    if not isinstance(levels, list):
        return parsed
    for item in levels:
        if not isinstance(item, (list, tuple)) or len(item) < 2:
            continue
        parsed.append([float(item[0]), float(item[1])])
    return parsed


def _extract_market_metadata(row: dict) -> dict[str, str]:
    return {
        "condition_id": str(row.get("condition_id") or "").strip(),
        "event_id": str(row.get("event_id") or "").strip(),
        "question": str(row.get("question") or "").strip(),
        "slug": str(row.get("slug") or "").strip(),
    }


def _distinct_condition_ids(rows: list[dict]) -> set[str]:
    return {
        str(row.get("condition_id") or "").strip()
        for row in rows
        if str(row.get("condition_id") or "").strip()
    }
