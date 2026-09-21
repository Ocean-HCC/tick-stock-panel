"""Tencent plugin registration, batching and existing service integration."""
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import httpx
import pytest
import yaml

from app.plugins.tencent import provider as module


def test_capabilities_and_availability_do_not_fetch():
    api = MagicMock()
    provider = module.TencentProvider(client=api)
    assert set(provider.config.datasets) == {"depth5"}
    assert module.availability()[0] is True
    api.fetch.assert_not_called()
    provider.close()
    api.close.assert_called_once()


def test_batching_deduplication_and_partial_failure(monkeypatch):
    api = MagicMock()
    api.fetch.side_effect = [httpx.ReadTimeout("timeout"), "valid"]
    monkeypatch.setattr(module, "parse_depth", lambda text, mapping: {
        value: {"bid_volumes": [1] * 5} for value in mapping.values()
    })
    provider = module.TencentProvider(client=api)
    symbols = [f"600{i:03d}.SH" for i in range(51)]
    rows = provider.get_depth_batch([*symbols, symbols[0], "920001.BJ"])
    assert set(rows) == {symbols[-1]}
    assert [len(call.args[0]) for call in api.fetch.call_args_list] == [50, 1]


def test_empty_or_unsupported_does_not_request():
    api = MagicMock()
    provider = module.TencentProvider(client=api)
    assert provider.get_depth_batch([]) == {}
    assert provider.get_depth_batch(["920001.BJ"]) == {}
    api.fetch.assert_not_called()


def test_rate_limit_stops_remaining_batches():
    api = MagicMock()
    response = httpx.Response(429, request=httpx.Request("GET", "https://qt.gtimg.cn"))
    api.fetch.side_effect = httpx.HTTPStatusError("rate limited", request=response.request, response=response)
    provider = module.TencentProvider(client=api)
    assert provider.get_depth_batch([f"600{i:03d}.SH" for i in range(101)]) == {}
    assert api.fetch.call_count == 1


def test_probe_reports_empty_as_failure_and_partial_results(monkeypatch):
    provider = module.TencentProvider(client=MagicMock())
    monkeypatch.setattr(provider, "get_depth_batch", lambda symbols: {})
    assert provider.test_dataset("depth5")["error"]
    monkeypatch.setattr(provider, "get_depth_batch", lambda symbols: {"600519.SH": {"timestamp": 1}})
    result = provider.test_dataset("depth5", ["600519.SH", "000001.SZ"])
    assert result["rows"] == 1
    assert result["preview"][0]["symbol"] == "600519.SH"
    assert result["missing_symbols"] == ["000001.SZ"]
    with pytest.raises(ValueError):
        provider.test_dataset("realtime")


def test_manifest_registration_and_only_depth_candidate(monkeypatch):
    from app.data_providers.capabilities import build_capability_matrix
    from app.data_providers.custom import loader

    manifest = yaml.safe_load((Path(module.__file__).parent / "plugin.yaml").read_text())
    monkeypatch.setattr(loader, "_PROVIDERS", {})
    monkeypatch.setattr(loader, "_PLUGIN_STATUS", {})
    loader._register_one_plugin(manifest)
    provider = loader.get_provider("tencent")
    try:
        assert loader.provider_has_dataset("tencent", "depth5")
        assert not loader.provider_has_dataset("tencent", "realtime")
        matrix = build_capability_matrix({"depth5_data_provider": "tencent"}, tickflow_tier="none")
        for cap in matrix["capabilities"]:
            names = {item["name"] for item in cap["candidates"]}
            assert ("tencent" in names) is (cap["id"] == "depth5")
            if cap["id"] == "depth5":
                assert cap["usable"]
    finally:
        provider.close()


def test_existing_depth_service_routes_to_plugin(monkeypatch):
    from app.data_providers import custom
    from app.services import preferences
    from app.services.depth_service import DepthService
    from app.tickflow.capabilities import Cap, CapabilityLimits, CapabilitySet

    plugin = module.TencentProvider(client=MagicMock())
    expected = {"600519.SH": {"ask_volumes": [0] * 5, "bid_volumes": [10] * 5}}
    monkeypatch.setattr(plugin, "get_depth_batch", lambda symbols: expected)
    monkeypatch.setattr(preferences, "get_depth5_data_provider", lambda: "tencent")
    monkeypatch.setattr(custom, "get_provider", lambda name: plugin)
    monkeypatch.setattr(custom, "provider_has_dataset", lambda name, ds: ds in plugin.config.datasets)
    service = DepthService()
    service._app_state = SimpleNamespace(capabilities=CapabilitySet({
        Cap.DEPTH5_BATCH: CapabilityLimits(batch=100, rpm=30),
    }))
    assert service._call_depth_batch(["600519.SH"]) == expected


def test_existing_probe_api_uses_plugin_without_writes(monkeypatch):
    from app.api import settings as api
    from app.data_providers import custom

    provider = module.TencentProvider(client=MagicMock())
    monkeypatch.setattr(provider, "get_depth_batch", lambda symbols: {
        "600519.SH": {"bid_volumes": [10] * 5, "ask_volumes": [0] * 5},
    })
    monkeypatch.setattr(custom, "get_provider", lambda name: provider)
    monkeypatch.setattr("app.services.preferences.save", lambda *args: pytest.fail("no writes"))
    result = api.test_data_source(api.CustomSourceTestIn(
        provider="tencent", dataset="depth5", symbols=["600519.SH"],
    ))
    assert result["rows"] == 1
    assert result["preview"][0]["bid_volumes"][0] == 10


def test_sealed_cache_keeps_lot_units_and_age_on_empty_update(monkeypatch):
    from datetime import date

    import polars as pl

    from app.services.depth_service import DepthService

    service = DepthService()
    day = date(2026, 9, 21)
    service.set_repo(SimpleNamespace(get_enriched_latest=lambda: (pl.DataFrame({
        "symbol": ["600519.SH"], "signal_limit_up": [True], "signal_limit_down": [False],
    }), day)))
    book = {"600519.SH": {
        "bid_volumes": [10, 0, 0, 0, 0], "ask_volumes": [0] * 5,
        "timestamp": 1789956000000,
    }}
    monkeypatch.setattr(service, "_call_depth_batch", lambda symbols: book)
    notify = MagicMock()
    monkeypatch.setattr(service, "_notify_depth_updated", notify)
    service._fetch_and_seal()
    assert service.get_sealed_map(day, False)["600519.SH"]["sealed"] is True
    assert service.get_sealed_map(day, False)["600519.SH"]["vol"] == 10
    fetched = service._sealed_fetched_ts
    monkeypatch.setattr(service, "_call_depth_batch", lambda symbols: {})
    service._fetch_and_seal()
    assert service._sealed_fetched_ts == fetched
    assert notify.call_count == 1
