"""Depth-only adapter for the existing plugin loader and independent depth route."""
from __future__ import annotations

import logging
from dataclasses import dataclass, field

import httpx

from app.plugins.tencent.client import MAX_BATCH, TencentClient, parse_depth, request_code

logger = logging.getLogger(__name__)


@dataclass
class _TencentConfig:
    name: str = "tencent"
    display_name: str = "腾讯财经(五档盘口)"
    datasets: dict = field(default_factory=lambda: {"depth5": None})
    path: None = None
    builtin: bool = True


def availability() -> tuple[bool, str]:
    # Startup must not make network calls or imply that the upstream is reachable.
    return True, "无需额外依赖; 网络与数据有效性请通过试拉确认"


class TencentProvider:
    name = "tencent"
    builtin = True

    def __init__(self, *, client: TencentClient | None = None) -> None:
        self.config = _TencentConfig()
        self._client = client if client is not None else TencentClient()

    def close(self) -> None:
        self._client.close()

    def get_depth_batch(self, symbols: list[str]) -> dict[str, dict]:
        mapping = {}
        for symbol in dict.fromkeys(symbols):
            try:
                mapping[request_code(symbol)] = symbol
            except ValueError:
                logger.warning("腾讯五档跳过不支持的标的 %s", symbol)
        codes = list(mapping)
        result = {}
        # The shared depth service may pass >50 symbols and has TickFlow-oriented
        # limits. Enforce Tencent's conservative request budget inside this adapter.
        for start in range(0, len(codes), MAX_BATCH):
            batch = codes[start:start + MAX_BATCH]
            try:
                text = self._client.fetch(batch)
                result.update(parse_depth(text, {code: mapping[code] for code in batch}))
            except httpx.HTTPStatusError as exc:
                logger.warning("腾讯五档 HTTP %s, 批次未更新", exc.response.status_code)
                if exc.response.status_code in (403, 429):
                    break
            except (httpx.RequestError, UnicodeError) as exc:
                logger.warning("腾讯五档批次请求失败: %s", type(exc).__name__)
            except RuntimeError as exc:
                logger.warning("腾讯五档批次停止: %s", exc)
                break
        return result

    def test_dataset(self, dataset: str, symbols: list[str] | None = None) -> dict:
        if dataset != "depth5":
            raise ValueError(f"腾讯插件仅支持 depth5, 不支持 {dataset}")
        requested = list(dict.fromkeys(symbols if symbols is not None else ["600519.SH"]))
        rows = self.get_depth_batch(requested)
        preview = [{"symbol": symbol, **book} for symbol, book in list(rows.items())[:5]]
        result = {
            "provider": self.name, "dataset": dataset, "rows": len(rows),
            "columns": list(preview[0]) if preview else [], "preview": preview,
            "missing_symbols": [symbol for symbol in requested if symbol not in rows],
        }
        if not rows:
            result["error"] = "未取得有效五档: 请检查沪深代码、行情时间、网络或访问限制; 不支持跨日历史试拉。"
        return result
