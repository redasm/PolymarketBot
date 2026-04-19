from __future__ import annotations

import sys
import types

import pytest

from polymarket_arb.client_factory import build_trading_client
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

    cfg = make_test_config(dry_run=False, signature_type=2, funder_address="0xabc123456789")
    client = build_trading_client(cfg)

    assert client is not None
    assert len(calls) == 2
    assert calls[0]["signature_type"] == 2
    assert calls[0]["funder"] == "0xabc123456789"
    assert calls[1]["creds"] == {"api_key": "k", "api_secret": "s", "api_passphrase": "p"}


def test_build_trading_client_raises_when_creds_missing(monkeypatch):
    class _FakeClobClient:
        def __init__(self, *args, **kwargs):
            pass

        def create_or_derive_api_creds(self):
            return None

    fake_module = types.ModuleType("py_clob_client.client")
    fake_module.ClobClient = _FakeClobClient
    monkeypatch.setitem(sys.modules, "py_clob_client.client", fake_module)

    cfg = make_test_config(dry_run=False, signature_type=2, funder_address="0xabc123456789")

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

    cfg = make_test_config(dry_run=False, signature_type=2, funder_address="0xabc123456789")
    client = build_trading_client(cfg)

    assert client is not None
    assert "http2=False" in calls
