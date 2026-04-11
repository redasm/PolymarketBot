"""时间工具函数：统一使用毫秒时间戳."""

import time

def now_ms() -> int:
    """当前 Unix 时间戳（毫秒）."""
    return int(time.time() * 1000)


def floor_ts(ts_ms: int, interval_ms: int) -> int:
    """将时间戳向下对齐到最近的 interval 边界."""
    assert interval_ms > 0, "interval_ms must be positive"
    return (ts_ms // interval_ms) * interval_ms


def ms_to_sec(ts_ms: int) -> float:
    return ts_ms / 1000.0


def sec_to_ms(ts_sec: float) -> int:
    return int(ts_sec * 1000)


INTERVAL_1S = 1_000
INTERVAL_5S = 5_000
INTERVAL_1M = 60_000
INTERVAL_5M = 300_000
INTERVAL_15M = 900_000
