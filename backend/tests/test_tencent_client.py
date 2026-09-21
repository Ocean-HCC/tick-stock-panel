"""Tencent wire-format tests: synthetic fixtures, no network or user configuration."""
from datetime import datetime

import httpx
import pytest

from app.market_time import CN_TZ
from app.plugins.tencent import client

NOW = datetime(2026, 9, 21, 10, 42, 20, tzinfo=CN_TZ)


def quote(code="sh600519", **changes):
    fields = ["0"] * 51
    fields[1:3] = ["测试证券", code[2:]]
    fields[6] = "1000"
    for i in range(5):
        fields[9 + 2 * i:11 + 2 * i] = [str(10 - i * .01), str(i + 1)]
        fields[19 + 2 * i:21 + 2 * i] = [str(10.01 + i * .01), str(i + 6)]
    fields[30] = "20260921104218"
    for index, value in changes.items():
        fields[int(index)] = value
    return f'v_{code}="{"~".join(fields)}";'


def test_depth_order_units_and_timestamp():
    row = client.parse_depth(quote(), {"sh600519": "600519.SH"}, now=NOW)["600519.SH"]
    assert row["bid_prices"] == [10, 9.99, 9.98, 9.97, 9.96]
    assert row["bid_volumes"] == [1, 2, 3, 4, 5]  # wire unit is lots, not shares
    assert row["ask_volumes"] == [6, 7, 8, 9, 10]
    assert row["timestamp"] == int(NOW.replace(second=18).timestamp() * 1000)


@pytest.mark.parametrize("code,symbol", [
    ("sh600519", "600519.SH"), ("sz000001", "000001.SZ"),
    ("sh510300", "510300.SH"), ("sz159915", "159915.SZ"),
])
def test_exchange_and_etf_mapping(code, symbol):
    assert client.request_code(symbol) == code
    assert symbol in client.parse_depth(quote(code), {code: symbol}, now=NOW)


@pytest.mark.parametrize("symbol", ["600519", "920001.BJ", "AAPL.US", "000001.SH", "600519.SH&x=1"])
def test_unsupported_symbols(symbol):
    with pytest.raises(ValueError):
        client.request_code(symbol)


@pytest.mark.parametrize("changes", [
    {"9": ""}, {"10": "-1"}, {"10": "NaN"}, {"19": "inf"},
    {"2": "000001"}, {"30": "invalid"}, {"30": "20260918150000"},
    {"30": "20260921103000"}, {"30": "20260921110000"},
    {"9": "0"}, {"6": "0"},
])
def test_invalid_row_never_becomes_zero_book(changes):
    assert client.parse_depth(quote(**changes), {"sh600519": "600519.SH"}, now=NOW) == {}


def test_zero_ask_is_valid_but_empty_book_is_not():
    zero_ask = {str(i): "0" for i in range(19, 29)}
    row = client.parse_depth(quote(**zero_ask), {"sh600519": "600519.SH"}, now=NOW)
    assert row["600519.SH"]["ask_volumes"] == [0] * 5
    all_zero = {str(i): "0" for i in range(9, 29)}
    assert client.parse_depth(quote(**all_zero), {"sh600519": "600519.SH"}, now=NOW) == {}


def test_partial_malformed_and_unsolicited_rows_are_isolated():
    text = quote(**{"10": ""}) + quote("sz000001") + quote("sh510300") + 'v_sh123456="";'
    rows = client.parse_depth(text, {"sh600519": "600519.SH", "sz000001": "000001.SZ"}, now=NOW)
    assert set(rows) == {"000001.SZ"}


@pytest.mark.parametrize("hour,minute,stamp,accepted", [
    (12, 30, "20260921113000", True),
    (16, 30, "20260921150000", True),
    (16, 30, "20260921103000", False),
    (13, 5, "20260921113000", False),
    (9, 0, "20260921104218", False),
])
def test_session_aware_freshness(hour, minute, stamp, accepted):
    rows = client.parse_depth(quote(**{"30": stamp}), {"sh600519": "600519.SH"},
                              now=NOW.replace(hour=hour, minute=minute))
    assert bool(rows) is accepted


def test_client_gbk_and_https(monkeypatch):
    calls = []
    monkeypatch.setattr(client, "_wait_for_slot", lambda: None)
    def respond(request):
        calls.append(request)
        return httpx.Response(200, content=quote().encode("gbk"))
    with httpx.Client(transport=httpx.MockTransport(respond)) as http:
        api = client.TencentClient(http=http)
        assert "测试证券" in api.fetch(["sh600519"])
    assert str(calls[0].url) == "https://qt.gtimg.cn/q=sh600519"


@pytest.mark.parametrize("status", [403, 429, 500])
def test_http_error_is_not_retried(monkeypatch, status):
    calls = []
    monkeypatch.setattr(client, "_wait_for_slot", lambda: None)
    monkeypatch.setattr(client, "_block_requests", lambda seconds: None)
    def respond(request):
        calls.append(request)
        return httpx.Response(status)
    with httpx.Client(transport=httpx.MockTransport(respond)) as http, pytest.raises(httpx.HTTPStatusError):
        client.TencentClient(http=http).fetch(["sh600519"])
    assert len(calls) == 1


def test_rate_gate_and_cooldown(monkeypatch):
    clock = [100.0]
    sleeps = []
    monkeypatch.setattr(client, "_next_request_at", 0.0)
    monkeypatch.setattr(client, "_blocked_until", 0.0)
    monkeypatch.setattr(client.time, "monotonic", lambda: clock[0])
    def sleep(seconds):
        sleeps.append(seconds)
        clock[0] += seconds
    monkeypatch.setattr(client.time, "sleep", sleep)
    client._wait_for_slot()
    client._wait_for_slot()
    assert sleeps == [3.0]
    client._block_requests(60)
    with pytest.raises(RuntimeError, match="冷却"):
        client._wait_for_slot()


def test_actual_429_cools_down_all_instances(monkeypatch):
    monkeypatch.setattr(client, "_next_request_at", 0.0)
    monkeypatch.setattr(client, "_blocked_until", 0.0)
    calls = []
    def respond(request):
        calls.append(request)
        return httpx.Response(429, headers={"Retry-After": "120"})
    with httpx.Client(transport=httpx.MockTransport(respond)) as http:
        first, second = client.TencentClient(http=http), client.TencentClient(http=http)
        with pytest.raises(httpx.HTTPStatusError):
            first.fetch(["sh600519"])
        with pytest.raises(RuntimeError, match="冷却"):
            second.fetch(["sz000001"])
    assert len(calls) == 1
    assert client._blocked_until > client.time.monotonic() + 115


@pytest.mark.parametrize("text", ["", "<html>denied</html>", 'v_sh600519="1~truncated";'])
def test_empty_or_changed_wire_format(text, caplog):
    assert client.parse_depth(text, {"sh600519": "600519.SH"}, now=NOW) == {}
    assert "无有效记录" in caplog.text


def test_weekend_and_previous_day_are_rejected():
    weekend = NOW.replace(day=26)
    assert client.parse_depth(quote(**{"30": "20260926104218"}),
                              {"sh600519": "600519.SH"}, now=weekend) == {}


def test_client_rejects_oversized_or_injected_request_before_network(monkeypatch):
    def wait():
        pytest.fail("invalid request must not reserve a slot")
    monkeypatch.setattr(client, "_wait_for_slot", wait)
    with httpx.Client(transport=httpx.MockTransport(lambda request: pytest.fail("no request"))) as http:
        api = client.TencentClient(http=http)
        assert api.fetch([]) == ""
        for codes in (["sh600519"] * 51, ["sh600519&x=1"]):
            with pytest.raises(ValueError):
                api.fetch(codes)
