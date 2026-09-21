"""Tencent web quotes: bounded HTTPS requests and strict five-level normalization.

Wire positions can be cross-checked against easyquotation/tencent.py. This is
not a vendor-guaranteed API. No retries, proxy rotation or cross-source fallback.
"""
from __future__ import annotations

import logging
import math
import re
import threading
import time
from datetime import datetime
from datetime import time as daytime

import httpx

from app.market_time import CN_TZ, cn_now

logger = logging.getLogger(__name__)
MAX_BATCH = 50
_RECORD = re.compile(r'v_((?:sh|sz)[0-9]{6})="([^"\r\n]*)";')
# First release: ordinary A shares + ETF code families, not indices/B shares/bonds.
_SYMBOL = re.compile(r"^((?:60|68)[0-9]{4}|5[0-9]{5})\.SH$|^((?:00|30)[0-9]{4}|15[0-9]{4})\.SZ$")
_rate_lock = threading.Lock()
_next_request_at = 0.0
_blocked_until = 0.0


def request_code(symbol: str) -> str:
    if not _SYMBOL.fullmatch(symbol):
        raise ValueError(f"腾讯五档暂不支持该证券代码: {symbol}")
    code, exchange = symbol.split(".")
    return exchange.lower() + code


def _wait_for_slot() -> None:
    """Shared across provider instances/reloads; never hold the lock during I/O."""
    global _next_request_at
    while True:
        with _rate_lock:
            now = time.monotonic()
            if now < _blocked_until:
                raise RuntimeError("腾讯行情访问限制冷却中, 请稍后试拉")
            wait = _next_request_at - now
            if wait <= 0:
                _next_request_at = now + 3.0
                return
        time.sleep(wait)


def _block_requests(seconds: float) -> None:
    global _blocked_until
    with _rate_lock:
        _blocked_until = max(_blocked_until, time.monotonic() + seconds)


class TencentClient:
    def __init__(self, *, http: httpx.Client | None = None) -> None:
        self._http = http if http is not None else httpx.Client(
            timeout=httpx.Timeout(5.0, connect=3.0),
            follow_redirects=False,
        )

    def close(self) -> None:
        self._http.close()

    def fetch(self, codes: list[str]) -> str:
        if not codes:
            return ""
        if len(codes) > MAX_BATCH or any(
            not re.fullmatch(r"(?:sh|sz)[0-9]{6}", code) for code in codes
        ):
            raise ValueError("腾讯请求必须为最多50个合法沪深代码")
        _wait_for_slot()
        response = self._http.get("https://qt.gtimg.cn/q=" + ",".join(codes))
        if response.status_code in (403, 429):
            # Do not re-send remaining batches after an access/rate-limit response.
            # Honor a longer numeric Retry-After, but never sleep through a cooldown.
            retry_after = response.headers.get("Retry-After", "")
            seconds = float(retry_after) if retry_after.isdigit() else 60.0
            _block_requests(max(60.0, seconds))
        response.raise_for_status()
        return response.content.decode("gb18030")


def _number(raw: str) -> float:
    value = float(raw)
    if not math.isfinite(value) or value < 0:
        raise ValueError("invalid nonnegative numeric field")
    return value


def _quote_time(raw: str, now: datetime) -> datetime:
    if not re.fullmatch(r"[0-9]{14}", raw):
        raise ValueError("invalid quote timestamp")
    stamp = datetime.strptime(raw, "%Y%m%d%H%M%S").replace(tzinfo=CN_TZ)
    if stamp.date() != now.date() or now.weekday() >= 5:
        raise ValueError("quote is not from today's weekday session")
    if stamp.time() < daytime(9, 15) or stamp.time() > daytime(15, 5):
        raise ValueError("quote outside supported session")
    if (stamp - now).total_seconds() > 5:
        raise ValueError("quote timestamp is in the future")
    # Freeze freshness at the lunch/closing boundary, not at local request time.
    reference = now
    if now.time() >= daytime(15):
        reference = now.replace(hour=15, minute=0, second=0, microsecond=0)
    elif daytime(11, 30) <= now.time() < daytime(13):
        reference = now.replace(hour=11, minute=30, second=0, microsecond=0)
    if (reference - stamp).total_seconds() > 180:
        raise ValueError("stale quote")
    return stamp


def parse_depth(
    text: str, requested: dict[str, str], *, now: datetime | None = None,
) -> dict[str, dict]:
    now = (now or cn_now()).astimezone(CN_TZ)
    result: dict[str, dict] = {}
    matched = 0
    for match in _RECORD.finditer(text):
        code, payload = match.groups()
        symbol = requested.get(code)
        if symbol is None:
            continue
        matched += 1
        fields = payload.split("~")
        try:
            if len(fields) < 31 or fields[2] != code[2:]:
                raise ValueError("truncated or mismatched quote")
            stamp = _quote_time(fields[30], now)
            # Suspended/no-trade snapshots must not be classified as sealed boards.
            if _number(fields[6]) == 0:
                raise ValueError("no trades in snapshot")
            bid_prices = [_number(fields[i]) for i in range(9, 19, 2)]
            bid_volumes = [_number(fields[i]) for i in range(10, 20, 2)]
            ask_prices = [_number(fields[i]) for i in range(19, 29, 2)]
            ask_volumes = [_number(fields[i]) for i in range(20, 30, 2)]
            for price, volume in zip(bid_prices + ask_prices, bid_volumes + ask_volumes, strict=True):
                if (price == 0) != (volume == 0):
                    raise ValueError("inconsistent empty price/volume level")
            if not any(bid_volumes + ask_volumes):
                raise ValueError("empty two-sided book")
            # Wire quantities are lots. Unlike easyquotation, do NOT multiply by 100.
            result[symbol] = {
                "bid_prices": bid_prices, "bid_volumes": bid_volumes,
                "ask_prices": ask_prices, "ask_volumes": ask_volumes,
                "timestamp": int(stamp.timestamp() * 1000),
            }
        except (ValueError, OverflowError) as exc:
            logger.warning("腾讯五档跳过 %s: %s", symbol, exc)
    if requested and not result:
        logger.warning("腾讯五档无有效记录 (请求%d只, 匹配%d条), 检查响应格式和行情时间", len(requested), matched)
    return result
