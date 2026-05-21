from __future__ import annotations

from polymarket_arb.quant_input_store import QuantInputStore


def test_quant_input_store_uses_inline_values_when_no_file_is_configured() -> None:
    store = QuantInputStore(logical_constraints_json='[{"a":1}]')

    snapshot = store.snapshot()

    assert snapshot.logical_constraints_json == '[{"a":1}]'
    assert snapshot.event_baselines_json == ""
    assert snapshot.input_metadata("logical_constraints")["source"] == "inline"
    assert snapshot.input_metadata("logical_constraints")["bytes"] == len('[{"a":1}]')


def test_quant_input_store_hot_reloads_valid_json_file(tmp_path) -> None:
    path = tmp_path / "wallet_observations.json"
    path.write_text('[{"wallet_address":"0xold"}]', encoding="utf-8")
    store = QuantInputStore(wallet_alpha_observations_file=str(path))

    assert store.snapshot().wallet_alpha_observations_json == '[{"wallet_address":"0xold"}]'

    path.write_text('[{"wallet_address":"0xnew"}]', encoding="utf-8")

    snapshot = store.snapshot()
    assert snapshot.wallet_alpha_observations_json == '[{"wallet_address":"0xnew"}]'
    metadata = snapshot.input_metadata("wallet_alpha_observations")
    assert metadata["source"] == "file"
    assert metadata["path"] == str(path)
    assert metadata["mtime_ns"] > 0
    assert len(metadata["sha256"]) == 64


def test_quant_input_store_keeps_last_valid_json_when_file_is_invalid(tmp_path) -> None:
    path = tmp_path / "rules.json"
    path.write_text('[{"subject_market_id":"a"}]', encoding="utf-8")
    store = QuantInputStore(logical_constraints_file=str(path))

    assert store.snapshot().logical_constraints_json == '[{"subject_market_id":"a"}]'

    path.write_text("{broken", encoding="utf-8")

    snapshot = store.snapshot()
    assert snapshot.logical_constraints_json == '[{"subject_market_id":"a"}]'
    assert snapshot.input_metadata("logical_constraints")["source"] == "cached_file"
