from __future__ import annotations

import threading
import time
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from datetime import date
from types import SimpleNamespace

import pytest
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient
from test_etf_history_extension import bars
from test_etf_history_extension import case as case

from app.api import data as data_api
from app.api import kline
from app.services import pipeline_jobs
from app.services.pipeline_jobs import JobCancelledError, JobStore
from app.services.quote_service import QuoteService


@pytest.fixture
def api_case(case, tmp_path, monkeypatch):
    _, repo, caps, _, _ = case
    store = JobStore(store_dir=tmp_path / "jobs")
    monkeypatch.setattr(pipeline_jobs, "job_store", store)
    monkeypatch.setattr(pipeline_jobs, "_run_slot_owner", None)
    monkeypatch.setattr(pipeline_jobs, "_CANCEL_FLAGS", {})
    app = FastAPI()
    app.include_router(kline.router)
    app.state.repo = repo
    app.state.capabilities = caps
    with TestClient(app) as client:
        yield client, store, app


def wait_job(store, job_id):
    deadline = time.monotonic() + 3
    while time.monotonic() < deadline:
        job = store.get(job_id)
        if job["status"] in ("succeeded", "failed") and pipeline_jobs._run_slot_owner is None:
            return job
        time.sleep(0.01)
    pytest.fail("worker did not complete")


def body(**kwargs):
    return dict(
        value=1, unit="month", asset_type="etf", expected_earliest_date="2025-09-22", **kwargs
    )


def test_api_publishes_and_keeps_frozen_plan(api_case):
    client, store, app = api_case
    entered = []

    @contextmanager
    def quiesced():
        entered.append("paused")
        yield
        entered.append("resumed")

    app.state.quote_service = SimpleNamespace(quiesced=quiesced)
    response = client.post("/api/kline/extend_history", json=body())
    assert response.status_code == 200
    job = wait_job(store, response.json()["job_id"])
    assert job["status"] == "succeeded"
    assert job["worker_active"] is False
    assert job["plan"]["requested_start"] == "2025-08-23"
    assert job["result"]["symbols_raw_unverified"] == 1
    assert entered == ["paused", "resumed"]
    assert data_api._safe_aggregate_etf_daily(app.state.repo)["storage"] == "etf"


@pytest.mark.parametrize(
    "change",
    [
        dict(value=True),
        dict(value=0),
        dict(value="2"),
        dict(value=1.5),
        dict(value=37),
        dict(unit="day"),
        dict(unit=[]),
        dict(asset_type="index"),
    ],
)
def test_api_rejects_invalid_etf_input(api_case, change):
    client, store, _ = api_case
    response = client.post("/api/kline/extend_history", json={**body(), **change})
    assert response.status_code == 400
    assert store.active_id() is None


def test_api_stale_and_reuse(api_case):
    client, store, app = api_case
    app.state.repo.append_etf_daily(bars([date(2025, 8, 25)]))
    response = client.post("/api/kline/extend_history", json=body())
    assert response.status_code == 409
    active, _ = store.create()
    response = client.post("/api/kline/extend_history", json=body())
    assert response.json() == {"status": "reused", "job_id": active}
    store.fail(active, "test finished")


def test_stock_two_fields_and_day_stay_compatible(api_case, monkeypatch):
    from app.services import extend_history

    client, store, _ = api_case
    received = []
    monkeypatch.setattr(
        extend_history,
        "run_extend_history",
        lambda repo, caps, value, unit, **kw: received.append((value, unit)) or {"daily_days": 1},
    )
    response = client.post("/api/kline/extend_history", json={"value": 2, "unit": "day"})
    assert wait_job(store, response.json()["job_id"])["status"] == "succeeded"
    assert received == [(2, "day")]


def test_cancel_retains_slot_and_late_checkpoint(api_case):
    _, store, app = api_case
    jid, _ = store.create(plan={"asset_type": "etf"})
    assert pipeline_jobs.try_acquire_run_slot(jid)
    store.start(jid)
    store.terminate(jid, "cancelled")
    try:
        assert not pipeline_jobs.try_acquire_run_slot("second")
        with pytest.raises(HTTPException) as exc:
            data_api.clear_data(SimpleNamespace(app=app))
        assert exc.value.status_code == 409
        store.checkpoint(jid, {"asset_type": "etf", "daily_rows_written": 42})
        assert store.get(jid)["status"] == "failed"
        assert store.get(jid)["worker_active"] is True
        assert store.get(jid)["result"]["daily_rows_written"] == 42
        with pytest.raises(JobCancelledError):
            store.progress(jid, "extend_etf_history", 50, "next safe boundary")
    finally:
        pipeline_jobs.release_run_slot(jid)
        store.finish_worker(jid)
    assert store.get(jid)["worker_active"] is False
    assert pipeline_jobs.try_acquire_run_slot("second")
    pipeline_jobs.release_run_slot("second")


def test_clear_refuses_pending_publication(api_case):
    _, _, app = api_case
    (app.state.repo.store.data_dir / ".etf_history_pending.json").write_text("{}")
    with pytest.raises(HTTPException) as exc:
        data_api.clear_data(SimpleNamespace(app=app))
    assert exc.value.status_code == 409


def test_quiescence_drains_inflight_and_blocks_new_fetches():
    qs = object.__new__(QuoteService)
    qs._fetch_lock = threading.Lock()
    qs._paused = False
    entered = threading.Event()

    def worker():
        with qs.quiesced():
            entered.set()
            assert qs._fetch_quotes() is False

    qs._fetch_lock.acquire()
    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(worker)
        try:
            assert not entered.wait(0.05)
            assert qs._paused
        finally:
            qs._fetch_lock.release()
        future.result(timeout=1)
    assert not qs._paused


def test_legacy_stats_are_not_independent_raw():
    repo = SimpleNamespace(
        execute_one=lambda sql: (
            (0,) if "FROM kline_etf_daily" in sql else (5, date(2016, 1, 1), date(2020, 1, 1), 1, 5)
        )
    )
    assert data_api._safe_aggregate_etf_daily(repo)["storage"] == "legacy_index"


def test_restarted_cancelled_worker_keeps_results_without_infinite_poll(tmp_path):
    store = JobStore(store_dir=tmp_path)
    jid, _ = store.create(plan={"asset_type": "etf"})
    store.start(jid)
    store.terminate(jid, "cancelled")
    store.checkpoint(jid, {"asset_type": "etf", "daily_rows_written": 3})
    restarted = JobStore(store_dir=tmp_path)
    assert restarted.get(jid)["worker_active"] is False
    assert restarted.get(jid)["result"]["daily_rows_written"] == 3
