"""CLOB 客户端工厂：构建认证后的交易客户端和只读客户端."""

from __future__ import annotations

import logging
from typing import Any

from polymarket_arb.config import ArbConfig

LOG = logging.getLogger(__name__)


def build_readonly_client(config: ArbConfig) -> Any:
    """构建只读 CLOB 客户端（无需私钥，用于读取订单簿）."""
    from py_clob_client.client import ClobClient

    client = ClobClient(config.clob_host, chain_id=config.chain_id)
    LOG.info("只读 CLOB 客户端已创建: %s", config.clob_host)
    return client


def build_trading_client(config: ArbConfig) -> Any:
    """构建带认证的交易 CLOB 客户端."""
    from py_clob_client.client import ClobClient
    from py_clob_client.exceptions import PolyApiException

    temp_client = _build_l1_client(config)
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

    client = ClobClient(
        config.clob_host,
        key=config.private_key,
        chain_id=config.chain_id,
        creds=creds,
        signature_type=config.signature_type,
        funder=config.funder_address,
    )
    LOG.info(
        "交易 CLOB 客户端已创建: host=%s, sig_type=%d, funder=%s…",
        config.clob_host,
        config.signature_type,
        config.funder_address[:10],
    )
    return client


def _build_l1_client(config: ArbConfig):
    from py_clob_client.client import ClobClient

    return ClobClient(
        config.clob_host,
        key=config.private_key,
        chain_id=config.chain_id,
        signature_type=config.signature_type,
        funder=config.funder_address,
    )


def _force_py_clob_http1() -> None:
    import httpx
    from py_clob_client.http_helpers import helpers as http_helpers

    current = getattr(http_helpers, "_http_client", None)
    if current is not None:
        try:
            current.close()
        except Exception:
            pass
    http_helpers._http_client = httpx.Client(http2=False)


def _create_or_derive_with_transport_fallback(temp_client: Any, config: ArbConfig):
    from py_clob_client.exceptions import PolyApiException

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
