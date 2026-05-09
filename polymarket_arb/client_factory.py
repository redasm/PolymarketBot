"""CLOB 客户端工厂：构建认证后的交易客户端和只读客户端."""

from __future__ import annotations

import logging
from typing import Any

from polymarket_arb.config import ArbConfig

LOG = logging.getLogger(__name__)


def _load_clob_client_class(config: ArbConfig) -> tuple[Any, str]:
    preferred = (config.clob_client_version or "auto").lower()
    if preferred in {"auto", "v2"}:
        try:
            from py_clob_client_v2 import ClobClient

            return ClobClient, "v2"
        except ImportError:
            if config.signature_type == 3:
                raise ImportError("POLYMARKET_SIGNATURE_TYPE=3 需要安装 py-clob-client-v2")
            if preferred == "v2":
                raise
    from py_clob_client.client import ClobClient

    return ClobClient, "v1"


def _resolve_signature_type(config: ArbConfig, version: str) -> Any:
    if version != "v2" or config.signature_type != 3:
        return config.signature_type

    try:
        from py_clob_client_v2 import SignatureTypeV2
    except ImportError:
        return config.signature_type
    return getattr(SignatureTypeV2, "POLY_1271", config.signature_type)


def _build_client(clob_client: Any, config: ArbConfig, **kwargs: Any) -> Any:
    base_kwargs = {"chain_id": config.chain_id}
    base_kwargs.update(kwargs)
    candidates = [
        base_kwargs,
        {k: v for k, v in base_kwargs.items() if k in {"chain_id", "key", "creds"}},
        {k: v for k, v in base_kwargs.items() if k in {"chain_id", "key"}},
        {k: v for k, v in base_kwargs.items() if k == "chain_id"},
    ]
    last_error: TypeError | None = None
    for candidate in candidates:
        try:
            return clob_client(config.clob_host, **candidate)
        except TypeError as exc:
            last_error = exc
    raise last_error or TypeError("无法构建 CLOB 客户端")


def build_readonly_client(config: ArbConfig) -> Any:
    """构建只读 CLOB 客户端（无需私钥，用于读取订单簿）."""
    clob_client, version = _load_clob_client_class(config)
    if version == "v2":
        _force_py_clob_http1()

    client = _build_client(clob_client, config)
    LOG.info("只读 CLOB 客户端已创建: %s client=%s", config.clob_host, version)
    return client


def build_trading_client(config: ArbConfig) -> Any:
    """构建带认证的交易 CLOB 客户端."""
    try:
        from py_clob_client.exceptions import PolyApiException
    except ImportError:  # py-clob-client-v2 only installs a different package.
        PolyApiException = RuntimeError  # type: ignore[assignment]
    clob_client, version = _load_clob_client_class(config)
    if version == "v2":
        _force_py_clob_http1()
    signature_type = _resolve_signature_type(config, version)

    temp_client = _build_l1_client(config, clob_client=clob_client)
    try:
        creds = _create_or_derive_with_transport_fallback(temp_client, config)
    except Exception as exc:
        LOG.error(
            "创建/派生 API 凭证失败: host=%s sig_type=%d funder=%s… error=%s",
            config.clob_host,
            config.signature_type,
            config.funder_address[:10] if config.funder_address else "",
            exc,
        )
        if isinstance(exc, PolyApiException):
            raise
        raise
    if creds is None:
        raise ValueError(
            "无法创建或派生 CLOB API 凭证，请检查 PRIVATE_KEY / POLYMARKET_FUNDER / SIGNATURE_TYPE"
        )
    LOG.info("API 凭证已创建/派生")

    client = _build_client(
        clob_client,
        config,
        key=config.private_key,
        creds=creds,
        signature_type=signature_type,
        funder=config.funder_address,
    )
    LOG.info(
        "交易 CLOB 客户端已创建: host=%s, client=%s, sig_type=%d, funder=%s…",
        config.clob_host,
        version,
        config.signature_type,
        config.funder_address[:10],
    )
    return client


def _build_l1_client(config: ArbConfig, *, clob_client: Any | None = None):
    version = None
    if clob_client is None:
        clob_client, version = _load_clob_client_class(config)
    if version is None:
        version = "v2" if config.signature_type == 3 else "v1"
    return _build_client(
        clob_client,
        config,
        key=config.private_key,
        signature_type=_resolve_signature_type(config, version),
        funder=config.funder_address,
    )


_CLOB_HTTP_HELPER_MODULES = (
    "py_clob_client_v2.http_helpers.helpers",
    "py_clob_client.http_helpers.helpers",
)


def _force_py_clob_http1() -> int:
    """Replace the module-private `_http_client` of the CLOB SDK with an HTTP/1.1 client.

    Returns the count of modules successfully patched. The contract here is
    fragile by design: it pokes a private attribute on a third-party module
    because the published API offers no transport switch. If a future
    py_clob_client release renames or relocates `_http_client`, this returns 0
    and emits a WARNING — `tests/test_client_factory.py` then drives the
    contract test that catches such a regression at PR review.
    """
    import sys
    import httpx

    patched = 0
    seen_modules = 0
    for module_name in _CLOB_HTTP_HELPER_MODULES:
        http_helpers = sys.modules.get(module_name)
        if http_helpers is None:
            try:
                http_helpers = __import__(module_name, fromlist=["helpers"])
            except Exception:
                continue

        seen_modules += 1
        if not hasattr(http_helpers, "_http_client"):
            LOG.warning(
                "%s 已升级，缺少 `_http_client` 属性，HTTP/1.1 强制失败 — 检查 py_clob_client API",
                module_name,
            )
            continue

        current = getattr(http_helpers, "_http_client", None)
        if current is not None:
            try:
                current.close()
            except Exception:
                pass
        try:
            http_helpers._http_client = httpx.Client(http2=False)
            LOG.debug("已将 %s 切换为 HTTP/1.1 client", module_name)
            patched += 1
        except Exception as exc:
            LOG.warning("切换 %s 到 HTTP/1.1 失败: %s", module_name, exc)

    if seen_modules and patched == 0:
        LOG.error(
            "py_clob_client* 已加载但所有 _http_client 强制 HTTP/1.1 都失败 — 实盘可能受 HTTP/2 影响"
        )
    return patched


def _create_or_derive_with_transport_fallback(temp_client: Any, config: ArbConfig):
    if hasattr(temp_client, "create_or_derive_api_key"):
        return temp_client.create_or_derive_api_key()

    try:
        from py_clob_client.exceptions import PolyApiException
    except ImportError:
        PolyApiException = RuntimeError  # type: ignore[assignment]

    try:
        return temp_client.create_or_derive_api_creds()
    except PolyApiException as exc:
        message = str(exc)
        if "Request exception!" not in message:
            raise

        LOG.warning("创建 API 凭证遇到传输异常，切换为 HTTP/1.1 后重试一次")
        _force_py_clob_http1()
        retry_client = _build_l1_client(config)
        return retry_client.create_or_derive_api_creds()
