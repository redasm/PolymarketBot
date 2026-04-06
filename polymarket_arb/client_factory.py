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

    temp_client = ClobClient(
        config.clob_host,
        key=config.private_key,
        chain_id=config.chain_id,
    )
    creds = temp_client.derive_api_key()
    LOG.info("API 凭证已派生")

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
