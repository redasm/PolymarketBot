from __future__ import annotations

import sys
import types

import pytest

from polymarket_arb.client_factory import _force_py_clob_http1, build_trading_client
from tests.conftest import make_test_config


def test_build_trading_client_uses_create_or_derive_api_creds(monkeypatch):
    calls: list[dict] = []

    class _FakeClobClient:
        def __init__(self, host, chain_id=None, key=None, creds=None, signature_type=None, funder=None, **kwargs):
            calls.append(
                {
                    "host": host,
                    "chain_id": chain_id,
                    "key": key,
                    "creds": creds,
                    "signature_type": signature_type,
                    "funder": funder,
                }
            )
            self._creds = {"api_key": "k", "api_secret": "s", "api_passphrase": "p"}

        def create_or_derive_api_creds(self):
            return self._creds

    fake_module = types.ModuleType("py_clob_client.client")
    fake_module.ClobClient = _FakeClobClient
    monkeypatch.setitem(sys.modules, "py_clob_client.client", fake_module)

    # dry_run=True 仍走完整 trading client path（shadow / readonly 也会调用）。
    # 实盘必须 v2 已由 test_build_trading_client_rejects_v1_in_live_mode 覆盖。
    cfg = make_test_config(dry_run=True, signature_type=2, funder_address="0xabc123456789")
    client = build_trading_client(cfg)

    assert client is not None
    assert len(calls) == 2
    assert calls[0]["signature_type"] == 2
    assert calls[0]["funder"] == "0xabc123456789"
    assert calls[1]["creds"] == {"api_key": "k", "api_secret": "s", "api_passphrase": "p"}


def test_build_trading_client_supports_v2_api_key_method(monkeypatch):
    calls: list[dict] = []

    class _FakeV2ClobClient:
        def __init__(self, host, chain_id=None, key=None, creds=None, signature_type=None, funder=None, **kwargs):
            calls.append(
                {
                    "host": host,
                    "chain_id": chain_id,
                    "key": key,
                    "creds": creds,
                    "signature_type": signature_type,
                    "funder": funder,
                }
            )
            self._creds = {"api_key": "v2-k", "api_secret": "v2-s", "api_passphrase": "v2-p"}

        def create_or_derive_api_key(self):
            return self._creds

    fake_module = types.ModuleType("py_clob_client_v2")
    fake_module.ClobClient = _FakeV2ClobClient
    monkeypatch.setitem(sys.modules, "py_clob_client_v2", fake_module)

    cfg = make_test_config(
        dry_run=False,
        clob_client_version="v2",
        signature_type=2,
        funder_address="0xabc123456789",
    )
    client = build_trading_client(cfg)

    assert client is not None
    assert len(calls) == 2
    assert calls[0]["key"] == "0xdead"
    assert calls[1]["creds"] == {"api_key": "v2-k", "api_secret": "v2-s", "api_passphrase": "v2-p"}


def test_build_trading_client_uses_poly_1271_for_deposit_wallet(monkeypatch):
    calls: list[dict] = []
    poly_1271 = object()

    class _FakeV2ClobClient:
        def __init__(self, host, chain_id=None, key=None, creds=None, signature_type=None, funder=None, **kwargs):
            calls.append(
                {
                    "host": host,
                    "chain_id": chain_id,
                    "key": key,
                    "creds": creds,
                    "signature_type": signature_type,
                    "funder": funder,
                }
            )
            self._creds = {"api_key": "v2-k", "api_secret": "v2-s", "api_passphrase": "v2-p"}

        def create_or_derive_api_key(self):
            return self._creds

    fake_module = types.ModuleType("py_clob_client_v2")
    fake_module.ClobClient = _FakeV2ClobClient
    fake_module.SignatureTypeV2 = types.SimpleNamespace(POLY_1271=poly_1271)
    monkeypatch.setitem(sys.modules, "py_clob_client_v2", fake_module)

    cfg = make_test_config(
        dry_run=False,
        clob_client_version="v2",
        signature_type=3,
        funder_address="0xdeposit",
    )
    client = build_trading_client(cfg)

    assert client is not None
    assert len(calls) == 2
    assert calls[0]["signature_type"] is poly_1271
    assert calls[0]["funder"] == "0xdeposit"
    assert calls[1]["signature_type"] is poly_1271
    assert calls[1]["funder"] == "0xdeposit"


def test_build_trading_client_raises_when_creds_missing(monkeypatch):
    class _FakeClobClient:
        def __init__(self, *args, **kwargs):
            pass

        def create_or_derive_api_creds(self):
            return None

    fake_module = types.ModuleType("py_clob_client.client")
    fake_module.ClobClient = _FakeClobClient
    monkeypatch.setitem(sys.modules, "py_clob_client.client", fake_module)

    cfg = make_test_config(dry_run=True, signature_type=2, funder_address="0xabc123456789")

    with pytest.raises(ValueError, match="无法创建或派生 CLOB API 凭证"):
        build_trading_client(cfg)


def test_build_trading_client_retries_with_http1_after_request_exception(monkeypatch):
    calls: list[str] = []

    class _PolyApiException(Exception):
        pass

    class _FakeClobClient:
        attempt = 0

        def __init__(self, *args, **kwargs):
            pass

        def create_or_derive_api_creds(self):
            _FakeClobClient.attempt += 1
            if _FakeClobClient.attempt == 1:
                raise _PolyApiException("PolyApiException[status_code=None, error_message=Request exception!]")
            return {"api_key": "k", "api_secret": "s", "api_passphrase": "p"}

    fake_client_module = types.ModuleType("py_clob_client.client")
    fake_client_module.ClobClient = _FakeClobClient
    monkeypatch.setitem(sys.modules, "py_clob_client.client", fake_client_module)

    fake_exc_module = types.ModuleType("py_clob_client.exceptions")
    fake_exc_module.PolyApiException = _PolyApiException
    monkeypatch.setitem(sys.modules, "py_clob_client.exceptions", fake_exc_module)

    class _FakeHttpClient:
        def close(self):
            calls.append("close")

    fake_helpers_module = types.ModuleType("py_clob_client.http_helpers.helpers")
    fake_helpers_module._http_client = _FakeHttpClient()
    monkeypatch.setitem(sys.modules, "py_clob_client.http_helpers.helpers", fake_helpers_module)

    import httpx

    class _FakeNewClient:
        def __init__(self, http2=False):
            calls.append(f"http2={http2}")

    monkeypatch.setattr(httpx, "Client", _FakeNewClient)

    cfg = make_test_config(dry_run=True, signature_type=2, funder_address="0xabc123456789")
    client = build_trading_client(cfg)

    assert client is not None
    assert "http2=False" in calls


def test_build_trading_client_rejects_v1_in_live_mode(monkeypatch):
    """Live mode 必须使用 py-clob-client-v2（CLOB V2 已于 2026-04-28 上线）。

    如果 py_clob_client_v2 缺失而 client_factory 静默回退到 v1，实盘下单会
    因 EIP-712 domain version / Order struct mismatch 全数失败。该测试锁住
    "live + v1 必须 raise" 的契约，防止回归。
    """
    class _FakeV1ClobClient:
        def __init__(self, *args, **kwargs):
            pass

        def create_or_derive_api_creds(self):
            return {"api_key": "k", "api_secret": "s", "api_passphrase": "p"}

    # 显式让 v2 不可导入，触发 client_factory 的 auto → v1 fallback。
    monkeypatch.setitem(sys.modules, "py_clob_client_v2", None)
    fake_v1 = types.ModuleType("py_clob_client.client")
    fake_v1.ClobClient = _FakeV1ClobClient
    monkeypatch.setitem(sys.modules, "py_clob_client.client", fake_v1)

    cfg = make_test_config(dry_run=False, signature_type=2, funder_address="0xabc123456789")

    with pytest.raises(RuntimeError, match="py-clob-client-v2"):
        build_trading_client(cfg)


def test_force_py_clob_http1_contract_attribute_present(monkeypatch):
    """Lock in the contract: the helper module exposes a writable
    `_http_client` attribute that we can swap out for an HTTP/1.1 httpx client.

    If a future `py_clob_client_v2` release renames or removes that attribute
    this test must fail loudly so we re-validate the live deposit-wallet flow
    before bumping the dependency.
    """
    import httpx

    fake_helpers = types.ModuleType("py_clob_client_v2.http_helpers.helpers")
    fake_helpers._http_client = httpx.Client(http2=False)
    monkeypatch.setitem(sys.modules, "py_clob_client_v2.http_helpers.helpers", fake_helpers)
    # Make sure the v1 path is missing so we only count the v2 patch.
    monkeypatch.setitem(sys.modules, "py_clob_client.http_helpers.helpers", types.ModuleType("dummy"))

    captured: list[bool] = []

    class _SpyClient:
        def __init__(self, http2=False):
            captured.append(http2)

        def close(self):
            pass

    monkeypatch.setattr(httpx, "Client", _SpyClient)

    patched = _force_py_clob_http1()

    # v2 module patched ok, v1 module had no `_http_client` -> skipped.
    assert patched == 1
    assert captured == [False]
    assert isinstance(fake_helpers._http_client, _SpyClient)


def test_force_py_clob_http1_contract_warns_when_attribute_missing(monkeypatch, caplog):
    """If a future SDK version removes `_http_client`, surface a WARNING
    rather than silently shipping HTTP/2 in production.
    """
    fake_helpers = types.ModuleType("py_clob_client_v2.http_helpers.helpers")
    monkeypatch.setitem(sys.modules, "py_clob_client_v2.http_helpers.helpers", fake_helpers)
    monkeypatch.setitem(sys.modules, "py_clob_client.http_helpers.helpers", types.ModuleType("dummy"))

    with caplog.at_level("WARNING"):
        patched = _force_py_clob_http1()

    assert patched == 0
    assert any("_http_client" in record.getMessage() for record in caplog.records)
