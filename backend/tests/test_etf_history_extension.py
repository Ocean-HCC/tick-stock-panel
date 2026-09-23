from datetime import date

import polars as pl
import pytest

from app.services import index_sync
from app.services.extend_history import compute_offset
from app.services.kline_sync import sync_daily_batch as real_sync_daily_batch
from app.tickflow.capabilities import Cap, CapabilityLimits, CapabilitySet
from app.tickflow.repository import DataStore, KlineRepository


def bars(days, symbol="510300.SH"):
    return pl.DataFrame(
        {
            "symbol": [symbol] * len(days),
            "date": days,
            "open": [4.0] * len(days),
            "high": [5.0] * len(days),
            "low": [3.0] * len(days),
            "close": [4.0] * len(days),
            "volume": [100.0] * len(days),
            "amount": [400.0] * len(days),
        }
    )


@pytest.fixture
def case(tmp_path, monkeypatch):
    from app.services import etf_history

    repo = KlineRepository(DataStore(tmp_path))
    repo.save_etf_instruments(pl.DataFrame({"symbol": ["510300.SH"]}))
    repo.append_etf_daily(bars([date(2025, 9, 22), date(2025, 9, 23)]))
    capset = CapabilitySet({Cap.KLINE_DAILY_BATCH: CapabilityLimits(batch=100, rpm=60)})
    client = object()
    monkeypatch.setattr(index_sync, "get_client", lambda: client)
    monkeypatch.setattr(index_sync.preferences, "get_daily_data_provider", lambda: "tickflow")
    monkeypatch.setattr(index_sync.preferences, "get_adj_factor_provider", lambda: "fuyao")
    monkeypatch.setattr(index_sync.preferences, "get_index_daily_batch_size", lambda: 100)
    monkeypatch.setattr(index_sync, "sleep_between_batches", lambda *a: None)
    calls = []

    def download(symbols, **kwargs):
        calls.append((symbols, kwargs))
        return bars([date(2025, 8, 25), date(2025, 9, 22)])

    monkeypatch.setattr(index_sync.kline_sync, "sync_daily_batch", download)
    monkeypatch.setattr(
        index_sync.kline_sync,
        "sync_adj_factor",
        lambda *a, **k: pytest.fail("must not fetch factors"),
    )
    plan = etf_history.prepare_extension(repo, 1, "month", "2025-09-22")
    return etf_history, repo, capset, plan, calls


def test_dates():
    assert date(2013, 9, 18) - compute_offset(6, "month") == date(2013, 3, 22)
    assert date(2025, 9, 15) - compute_offset(9, "year") == date(2016, 9, 17)


def test_etf_screener_cache_tracks_published_generation(case):
    from app.services.screener import ScreenerService

    service, repo, caps, plan, _ = case
    repo.append_etf_enriched(bars([date(2025, 9, 22), date(2025, 9, 23)]))
    screener = ScreenerService(repo, asset_type="etf")
    before = screener._load_enriched_history(date(2025, 9, 23), 100)
    assert before.height == 2
    service.run(repo, caps, plan, lambda *a: None)
    after = screener._load_enriched_history(date(2025, 9, 23), 100)
    assert after.height == 3
    assert after["date"].min() == date(2025, 8, 25)


def test_raw_without_remote_factors(case):
    service, repo, caps, plan, calls = case
    log = []
    result = service.run(repo, caps, plan, lambda *args, **kwargs: log.append(args))
    assert result["outcome"] == "success"  # Weekend start is not evidence of a gap.
    assert result["earliest_after"] == "2025-08-25"
    assert result["enriched_rows_written"] == 3
    assert result["symbols_raw_unverified"] == 1
    assert result["coverage"][0]["price_basis"] == "raw_unverified"
    assert sum("未确认复权" in row[2] for row in log) == 1
    assert calls[0][1]["start_time"].date() == date(2025, 8, 23)
    assert calls[0][1]["end_time"].date() == date(2025, 9, 22)
    frame = pl.read_parquet(repo.store.data_dir / "kline_etf_enriched/date=2025-08-25/part.parquet")
    assert frame["close"][0] == frame["raw_close"][0] == 4.0
    assert not (repo.store.data_dir / "adj_factor_etf/all.parquet").exists()


def test_local_future_event_recalculates_complete_history(case):
    service, repo, caps, plan, _ = case
    path = repo.store.data_dir / "adj_factor_etf/all.parquet"
    pl.DataFrame(
        {"symbol": ["510300.SH"], "trade_date": [date(2025, 9, 23)], "ex_factor": [2.0]}
    ).write_parquet(path)
    before = path.read_bytes()
    result = service.run(repo, caps, plan, lambda *a, **k: None)
    assert result["symbols_with_local_factors"] == 1
    assert result["enriched_rows_written"] == 3
    assert path.read_bytes() == before
    frame = pl.read_parquet(repo.store.data_dir / "kline_etf_enriched/date=2025-08-25/part.parquet")
    assert frame["close"][0] == 2.0


def test_invalid_factor_preserves_data(case):
    service, repo, caps, plan, _ = case
    pl.DataFrame(
        {"symbol": ["510300.SH"], "trade_date": [date(2025, 9, 23)], "ex_factor": [-2.0]}
    ).write_parquet(repo.store.data_dir / "adj_factor_etf/all.parquet")
    result = service.run(repo, caps, plan, lambda *a, **k: None)
    assert result["outcome"] == "failed"
    assert result["earliest_after"] == plan["earliest_before"]


@pytest.mark.parametrize(
    "value,unit",
    [(True, "year"), (1.5, "year"), ("6", "month"), (37, "month"), (11, "year"), (1, "day")],
)
def test_invalid_range(case, value, unit):
    service, repo, _, _, _ = case
    with pytest.raises(ValueError):
        service.prepare_extension(repo, value, unit, "2025-09-22")


def test_stale_range(case):
    service, repo, _, _, _ = case
    with pytest.raises(service.StaleEtfRangeError):
        service.prepare_extension(repo, 1, "month", "2025-01-01")


def test_factor_change_aborts_publication(case, monkeypatch):
    service, repo, caps, plan, _ = case
    original = service.compute_enriched

    def changed(*a, **k):
        out = original(*a, **k)
        pl.DataFrame(
            {"symbol": ["510300.SH"], "trade_date": [date(2025, 9, 23)], "ex_factor": [2.0]}
        ).write_parquet(repo.store.data_dir / "adj_factor_etf/all.parquet")
        return out

    monkeypatch.setattr(service, "compute_enriched", changed)
    with pytest.raises(ValueError, match="数据已变化"):
        service.run(repo, caps, plan, lambda *a, **k: None)
    assert not (repo.store.data_dir / "kline_etf_daily/date=2025-08-25/part.parquet").exists()


def test_publication_recovery(case, monkeypatch):
    from app.enriched_generation import EnrichedGenerationUnavailableError, EnrichedPublication
    from app.tickflow import etf_history_store

    service, repo, caps, plan, _ = case
    original = etf_history_store.shutil.copyfile
    count = 0

    def broken(*a, **k):
        nonlocal count
        count += 1
        if count == 2:
            raise OSError("injected")
        return original(*a, **k)

    monkeypatch.setattr(etf_history_store.shutil, "copyfile", broken)
    with pytest.raises(OSError):
        service.run(repo, caps, plan, lambda *a, **k: None)
    with pytest.raises(EnrichedGenerationUnavailableError):
        repo.get_matrix_data_generation("etf")
    with pytest.raises(EnrichedGenerationUnavailableError):
        repo.get_etf_daily("510300.SH", date(2025, 8, 1), date(2025, 9, 30))
    with pytest.raises(EnrichedGenerationUnavailableError):
        repo.get_enriched_latest_asset("etf")
    with pytest.raises(EnrichedGenerationUnavailableError):
        repo.append_etf_daily(bars([date(2025, 8, 25)]))
    with pytest.raises(EnrichedGenerationUnavailableError):
        repo.flush_live_daily_asset("etf", bars([date(2025, 8, 25)]))
    with pytest.raises(EnrichedGenerationUnavailableError):
        EnrichedPublication(repo.store.data_dir, "etf", recover=True).begin()
    monkeypatch.setattr(etf_history_store.shutil, "copyfile", original)
    recovery = service.prepare_extension(repo, 1, "month", "2025-09-22")
    assert recovery["recovery_only"]
    result = service.run(repo, caps, recovery, lambda *a, **k: None)
    assert result["earliest_after"] == "2025-08-25"
    assert result["recovery_only"]
    assert repo.get_matrix_data_generation("etf")


@pytest.mark.parametrize("mode", ["empty", "unreadable", "schema"])
def test_unavailable_local_file_is_preserved(case, mode):
    service, repo, caps, plan, _ = case
    path = repo.store.data_dir / "adj_factor_etf/all.parquet"
    if mode == "unreadable":
        path.write_bytes(b"not-parquet")
    elif mode == "schema":
        pl.DataFrame({"wrong": [1]}).write_parquet(path)
    else:
        pl.DataFrame(
            schema={"symbol": pl.String, "trade_date": pl.Date, "ex_factor": pl.Float64}
        ).write_parquet(path)
    before = path.read_bytes()
    result = service.run(repo, caps, plan, lambda *a: None)
    assert result["outcome"] == "success"
    assert result["symbols_raw_unverified"] == 1
    assert result["warnings"]
    assert path.read_bytes() == before


def test_mixed_factors_partial_download_and_listing(case, monkeypatch):
    service, repo, caps, plan, _ = case
    symbols = ["510300.SH", "510500.SH", "159001.SZ", "159002.SZ"]
    repo.save_etf_instruments(
        pl.DataFrame({"symbol": symbols, "listing_date": [None, None, None, date(2025, 10, 1)]})
    )
    repo.append_etf_daily(pl.concat([bars([date(2025, 9, 22)], s) for s in symbols]))
    pl.DataFrame(
        {"symbol": [symbols[0]], "trade_date": [date(2025, 9, 23)], "ex_factor": [2.0]}
    ).write_parquet(repo.store.data_dir / "adj_factor_etf/all.parquet")
    monkeypatch.setattr(
        index_sync.kline_sync,
        "sync_daily_batch",
        lambda *a, **k: pl.concat(
            [bars([date(2025, 8, 25), date(2025, 9, 22)], s) for s in symbols[:2]]
        ),
    )
    result = service.run(repo, caps, plan, lambda *a: None)
    assert result["outcome"] == "partial"
    assert result["symbols_succeeded"] == 2
    assert result["symbols_failed"] == result["symbols_skipped"] == 1
    assert result["symbols_with_local_factors"] == result["symbols_raw_unverified"] == 1
    stored = repo.get_etf_daily(
        "510500.SH", date(2025, 8, 25), date(2025, 8, 25), ["close", "raw_close"]
    )
    assert stored.row(0) == (4.0, 4.0)
    assert result["failures"][0]["symbol"] == "159001.SZ"


def test_ordinary_etf_sync_keeps_return_and_batch_preference(case, monkeypatch):
    _, repo, caps, _, calls = case
    monkeypatch.setattr(index_sync.preferences, "get_index_daily_batch_size", lambda: 1)
    progress = []
    result = index_sync.sync_and_persist_etf_daily(
        repo,
        caps,
        symbols_override=["510300.SH", "510500.SH"],
        on_chunk_done=lambda *a: progress.append(a),
    )
    assert type(result) is int and result == 4
    assert [c[0] for c in calls] == [["510300.SH"], ["510500.SH"]]
    assert progress == [(1, 2), (2, 2)]


def test_raw_change_during_calculation_aborts(case, monkeypatch):
    service, repo, caps, plan, _ = case
    original = service.compute_enriched

    def changed(*a, **k):
        result = original(*a, **k)
        repo.append_etf_daily(bars([date(2025, 9, 24)]))
        return result

    monkeypatch.setattr(service, "compute_enriched", changed)
    with pytest.raises(ValueError, match="数据已变化"):
        service.run(repo, caps, plan, lambda *a: None)
    assert not (repo.store.data_dir / "kline_etf_daily/date=2025-08-25/part.parquet").exists()


def test_no_independent_raw_never_uses_stock_or_index(tmp_path):
    from app.services.etf_history import prepare_extension

    repo = KlineRepository(DataStore(tmp_path))
    repo.append_daily(bars([date(2020, 1, 1)]))
    repo.append_index_daily(bars([date(2021, 1, 1)]))
    with pytest.raises(ValueError, match="无独立 ETF"):
        prepare_extension(repo, 6, "month", "2021-01-01")


def test_changed_source_never_silently_falls_back(case, monkeypatch):
    service, repo, caps, plan, calls = case
    monkeypatch.setattr(index_sync.preferences, "get_daily_data_provider", lambda: "fuyao")
    with pytest.raises(ValueError, match="来源"):
        service.run(repo, caps, plan, lambda *a: None)
    assert calls == []


def test_unsettled_today_is_rejected(case, monkeypatch):
    from datetime import datetime

    from app.market_time import CN_TZ

    service, repo, _, _, _ = case
    monkeypatch.setattr(service, "cn_now", lambda: datetime(2025, 9, 23, 14, tzinfo=CN_TZ))
    with pytest.raises(ValueError, match="盘后"):
        service.prepare_extension(repo, 6, "month", "2025-09-22")


def test_daily_sdk_boundary_is_raw_and_beijing(case, monkeypatch):
    from datetime import datetime
    from types import SimpleNamespace

    from app.market_time import CN_TZ

    service, repo, caps, plan, _ = case
    from app.services import kline_sync

    captured = []

    def batch(symbols, **kwargs):
        captured.append(kwargs)
        return {
            "510300.SH": {
                "timestamp": [int(datetime(2025, 8, 25, tzinfo=CN_TZ).timestamp() * 1000)],
                "open": [4],
                "high": [5],
                "low": [3],
                "close": [4],
                "volume": [100],
                "amount": [400],
            }
        }

    client = SimpleNamespace(klines=SimpleNamespace(batch=batch))
    monkeypatch.setattr(index_sync, "get_client", lambda: client)
    monkeypatch.setattr(kline_sync, "get_client", lambda: client)
    monkeypatch.setattr(kline_sync, "sync_daily_batch", real_sync_daily_batch)
    result = service.run(repo, caps, plan, lambda *a: None)
    assert result["outcome"] == "success"
    assert captured[0]["adjust"] == "none"
    assert captured[0]["count"] == 10000
    assert (
        datetime.fromtimestamp(captured[0]["start_time"] / 1000, CN_TZ).isoformat()
        == "2025-08-23T00:00:00+08:00"
    )
