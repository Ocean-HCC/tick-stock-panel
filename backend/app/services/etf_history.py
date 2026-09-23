"""Historical ETF calculation using the ordinary ETF download and local factors."""

# ruff: noqa: RUF001 -- Chinese user-facing messages.
from __future__ import annotations

import copy
from contextlib import suppress
from datetime import date, datetime, time

import polars as pl

from app.indicators.pipeline import compute_enriched
from app.market_time import CN_TZ, cn_now
from app.services import index_sync
from app.tickflow.capabilities import Cap
from app.tickflow.etf_history_store import (
    fingerprint,
    pending_plan,
    publish_batch,
    raw_range,
    raw_snapshot,
    read_raw,
    recover_pending,
)


class StaleEtfRangeError(ValueError):
    pass


def validate_source(capset):
    if index_sync.preferences.get_daily_data_provider() != "tickflow":
        raise ValueError("ETF 沿用原同步的日线来源；当前日线来源与该路径不一致")
    if not capset.has(Cap.KLINE_DAILY_BATCH):
        raise ValueError("ETF 日线来源不可用，请检查数据源配置")


def prepare_extension(repo, value, unit, expected_earliest_date):
    from app.services.extend_history import compute_offset

    maximum = {"month": 36, "year": 10}.get(unit) if isinstance(unit, str) else None
    if type(value) is not int or maximum is None or not 1 <= value <= maximum:
        raise ValueError("ETF 扩展范围须为 1～36 月或 1～10 年")
    if not isinstance(expected_earliest_date, str):
        raise ValueError("缺少日期基准，请重新打开 ETF 设置")
    expected = date.fromisoformat(expected_earliest_date)
    unfinished = pending_plan(repo)
    if unfinished:
        return {**unfinished, "recovery_only": True}
    earliest, latest = raw_range(repo)
    if earliest is None:
        raise ValueError("本地无独立 ETF 日 K，请先同步 ETF")
    if expected != earliest:
        raise StaleEtfRangeError("ETF 范围已变化，请刷新后确认预计日期")
    now = cn_now()
    if latest > now.date() or (latest == now.date() and now.time() < time(16)):
        raise ValueError("ETF 最新日 K 尚未定版，请盘后再扩展")
    return {
        "asset_type": "etf",
        "earliest_before": earliest.isoformat(),
        "requested_start": (earliest - compute_offset(value, unit)).isoformat(),
        "requested_end": earliest.isoformat(),
        "history_end": latest.isoformat(),
        "daily_provider": "tickflow",
        "factor_source": "local_etf",
        "adjustment_mode": "local_if_available",
    }


def _local_factors(repo):
    empty = pl.DataFrame(
        schema={"symbol": pl.String, "trade_date": pl.Date, "ex_factor": pl.Float64}
    )
    frame = index_sync._load_etf_factors(repo)
    if frame.is_empty():
        return empty
    try:
        return frame.select(
            pl.col("symbol").cast(pl.String),
            pl.col("trade_date").cast(pl.Date, strict=False),
            pl.col("ex_factor").cast(pl.Float64, strict=False),
        )
    except (pl.exceptions.PolarsError, TypeError, ValueError):
        return empty


def _valid_bars(frame):
    required = ["symbol", "date", "open", "high", "low", "close", "volume", "amount"]
    if frame.is_empty() or not set(required).issubset(frame.columns):
        return False
    if frame.select(pl.any_horizontal(pl.col(required).is_null()).any()).item():
        return False
    prices = ["open", "high", "low", "close"]
    return not frame.filter(
        ~pl.col("symbol").str.contains(r"^\d{6}\.(SH|SZ|BJ)$")
        | pl.any_horizontal([~pl.col(c).is_finite() | (pl.col(c) <= 0) for c in prices])
        | pl.any_horizontal(
            [~pl.col(c).is_finite() | (pl.col(c) < 0) for c in ["volume", "amount"]]
        )
        | (pl.col("high") < pl.max_horizontal(prices))
        | (pl.col("low") > pl.min_horizontal(prices))
    ).height


def run(repo, capset, plan, emit, checkpoint=lambda result: None):
    result = dict(
        plan,
        outcome="partial",
        universe_size=0,
        daily_rows_written=0,
        enriched_rows_written=0,
        adj_factor_rows_written=0,
        symbols_succeeded=0,
        symbols_failed=0,
        symbols_skipped=0,
        symbols_with_local_factors=0,
        symbols_raw_unverified=0,
        failures=[],
        coverage=[],
        warnings=[],
        earliest_after=plan["earliest_before"],
    )
    checkpoint(result)
    if pending_plan(repo):
        emit("extend_etf_history", 5, "恢复上次中断的 ETF 批次…")
        recovered = recover_pending(repo)
        result.update(recovered or {})
        result.update(
            recovery_only=True, outcome="partial", earliest_after=raw_range(repo)[0].isoformat()
        )
        result["warnings"].append("已恢复上次批次，本次未继续前移")
        checkpoint(result)
        emit("extend_etf_history", 100, "已恢复上次批次，本次未继续前移")
        return result
    validate_source(capset)
    client = index_sync.get_client()
    if raw_range(repo) != (
        date.fromisoformat(plan["earliest_before"]),
        date.fromisoformat(plan["history_end"]),
    ):
        raise StaleEtfRangeError("排队期间 ETF 范围已变化，请重新提交")
    start, end, latest = (
        date.fromisoformat(plan[k]) for k in ("requested_start", "requested_end", "history_end")
    )
    instruments = repo.get_etf_instruments()
    if instruments.is_empty():
        index_sync.sync_etf_instruments(repo)
        instruments = repo.get_etf_instruments()
    if instruments.is_empty() or "symbol" not in instruments.columns:
        raise ValueError("ETF 维表为空，请先同步 ETF")
    symbols = sorted(set(instruments["symbol"].drop_nulls().to_list()))
    result["symbols"] = symbols
    result["universe_size"] = len(symbols)
    listing = {}
    if "listing_date" in instruments.columns:
        for symbol, listed in instruments.select("symbol", "listing_date").iter_rows():
            with suppress(ValueError, TypeError):
                listing[symbol] = date.fromisoformat(str(listed)[:10])
    eligible = [s for s in symbols if listing.get(s, start) <= end]
    result["symbols_skipped"] = len(symbols) - len(eligible)
    factor_path = repo.store.data_dir / "adj_factor_etf/all.parquet"
    factor_version = fingerprint(factor_path)
    factors = _local_factors(repo)
    raw_version = raw_snapshot(repo)
    if raw_range(repo) != (end, latest):
        raise StaleEtfRangeError("初始化期间 ETF 范围已变化，请重新提交")

    def verify():
        validate_source(capset)
        if index_sync.get_client() is not client:
            raise ValueError("ETF 数据源连接已变化，任务停止")
        if fingerprint(factor_path) != factor_version or raw_snapshot(repo) != raw_version:
            raise ValueError("ETF 数据已变化，任务停止，请重新提交")

    def before_batch(i, total):
        verify()
        emit("extend_etf_history", 5 + int(85 * i / total), f"ETF 日 K 批次 {i + 1}/{total}")

    verify()
    checkpoint(result)
    batches = index_sync.iter_etf_daily_batches(
        eligible,
        capset,
        datetime.combine(start, time.min, CN_TZ),
        datetime.combine(end, time.max, CN_TZ),
        before_batch=before_batch,
    )
    for i, total, chunk, incoming in batches:
        verify()
        valid, raw_parts, factor_parts = [], [], []
        old = read_raw(repo, chunk)
        if not incoming.is_empty() and "date" in incoming.columns:
            incoming = incoming.with_columns(pl.col("date").cast(pl.Date, strict=False))
            incoming = incoming.filter(
                pl.col("date").is_null() | pl.col("date").is_between(start, end)
            )
            if "symbol" in incoming.columns:
                incoming = incoming.unique(["symbol", "date"], keep="last")
        for symbol in chunk:
            bars = (
                incoming.filter(pl.col("symbol") == symbol)
                if "symbol" in incoming.columns
                else pl.DataFrame()
            )
            fs = factors.filter(
                (pl.col("symbol") == symbol)
                & ((pl.col("trade_date") <= latest) | pl.col("trade_date").is_null())
            )
            reason = None
            if not _valid_bars(bars):
                reason = "日 K 为空或数据无效，未写入"
            elif bars["date"].min() >= end:
                reason = "未返回更早日 K，上市日期或覆盖待确认"
            elif fs.filter(
                pl.col("trade_date").is_null()
                | pl.col("ex_factor").is_null()
                | ~pl.col("ex_factor").is_finite()
                | (pl.col("ex_factor") <= 0)
            ).height:
                reason = "本地因子数值或日期异常，保留原数据"
            if reason:
                result["failures"].append({"symbol": symbol, "reason": reason})
                continue
            previous = (
                old.filter(pl.col("symbol") == symbol) if not old.is_empty() else bars.clear()
            )
            merged = (
                pl.concat([previous, bars], how="diagonal_relaxed")
                .unique(["symbol", "date"], keep="last")
                .sort(["symbol", "date"])
            )
            if not _valid_bars(merged):
                result["failures"].append(
                    {"symbol": symbol, "reason": "已有原始历史无效，保留原数据"}
                )
                continue
            valid.append(symbol)
            raw_parts.append(merged)
            factor_parts.append(fs)
        result["symbols_failed"] = len(result["failures"])
        checkpoint(result)
        if not valid:
            continue
        raw, fs = (
            pl.concat(raw_parts, how="diagonal_relaxed"),
            pl.concat(factor_parts).unique(["symbol", "trade_date"], keep="last"),
        )
        emit("extend_etf_history", 5 + int(85 * i / total), f"重算 ETF 完整历史 {i + 1}/{total}")
        try:
            enriched = compute_enriched(raw, factors=fs, instruments=None)
        except Exception as exc:
            result["failures"].extend(
                {"symbol": s, "reason": f"指标计算失败（{type(exc).__name__}）"} for s in valid
            )
            result["symbols_failed"] = len(result["failures"])
            checkpoint(result)
            continue
        verify()
        emit("extend_etf_history", 5 + int(85 * i / total), f"发布 ETF 批次 {i + 1}/{total}")
        committed = copy.deepcopy(result)
        with_factors = set(fs["symbol"].to_list())
        committed["symbols_succeeded"] += len(valid)
        committed["symbols_with_local_factors"] += len(with_factors)
        committed["symbols_raw_unverified"] += len(valid) - len(with_factors)
        committed["daily_rows_written"] += incoming.filter(pl.col("symbol").is_in(valid)).height
        committed["enriched_rows_written"] += enriched.height
        committed["earliest_after"] = min(result["earliest_after"], raw["date"].min().isoformat())
        first_warning = not committed["warnings"]
        if first_warning:
            committed["warnings"].append("ETF 仅使用本地因子；缺因子时按原始价格计算，未确认复权")
        for symbol in valid:
            committed["coverage"].append(
                {
                    "symbol": symbol,
                    "earliest": raw.filter(pl.col("symbol") == symbol)["date"].min().isoformat(),
                    "price_basis": "local_factors_unverified"
                    if symbol in with_factors
                    else "raw_unverified",
                }
            )
        publish_batch(repo, raw, enriched, plan, committed, verify)
        result = committed
        raw_version = raw_snapshot(repo)
        result["earliest_after"] = raw_range(repo)[0].isoformat()
        checkpoint(result)
        if first_warning:
            emit("extend_etf_history", 5 + int(85 * (i + 1) / total), result["warnings"][0])
    result["outcome"] = (
        "failed"
        if not result["symbols_succeeded"]
        else "partial"
        if result["failures"]
        else "success"
    )
    checkpoint(result)
    emit("extend_etf_history", 100, f"ETF 扩展结束，实际最早 {result['earliest_after']}")
    return result
