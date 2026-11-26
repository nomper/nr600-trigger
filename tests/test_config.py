import json
from dataclasses import replace
from datetime import datetime, timedelta

import pytest

from src.config import MockConfig, cron_schedule, interval_next, load_config, parse_anchor, save_config


@pytest.mark.parametrize(("model", "offset"), [("NR-500", 0), ("NR-600", 4)])
def test_identifier(config, model, offset):
    for unit_id in range(4):
        assert replace(config, logger_model=model, unit_id=unit_id).identifier == offset + unit_id


@pytest.mark.parametrize(("model", "offset"), [("NR-500", 0), ("NR-600", 8)])
def test_expected_identifier(config, model, offset):
    for unit_id in range(4):
        assert replace(config, logger_model=model, unit_id=unit_id).expected_identifier == offset + unit_id


@pytest.mark.parametrize(("key", "value"), [
    ("version", True), ("version", 2), ("unit_id", True), ("unit_id", 4),
    ("unit_id", -1), ("logger_model", "NR-X"), ("lead_seconds", 2),
    ("lead_seconds", 6), ("lead_seconds", 3.0), ("log_retention_days", 0),
    ("visible", 1), ("progid", " "), ("settings_file", None),
    ("cron", " "), ("cron", None), ("cron", "* * * * *"), ("cron", "60 * * * * *"),
    ("cron", "0 0 0 31 2 *"), ("mock", None), ("mock", {"scenario": "unknown"}),
    ("mock", {"scenario": "delay", "delay_seconds": "1"}),
])
def test_invalid_json_settings(tmp_path, config, key, value):
    path = tmp_path / "config.json"
    save_config(config, path)
    data = json.loads(path.read_text(encoding="utf-8"))
    data[key] = value
    path.write_text(json.dumps(data), encoding="utf-8")
    with pytest.raises(ValueError, match=key):
        load_config(path)


@pytest.mark.parametrize("lead", [3, 4, 5])
def test_save_and_load_with_mock(tmp_path, config, lead):
    path = tmp_path / "config.json"
    expected = replace(config, lead_seconds=lead, mock=MockConfig("delay", 0.4))
    save_config(expected, path)
    assert load_config(path) == expected
    save_config(config, path)
    assert "mock" not in json.loads(path.read_text(encoding="utf-8"))
    assert load_config(path) == config


def test_defaults_and_required_fields(tmp_path, config):
    path = tmp_path / "config.json"
    save_config(config, path)
    values = json.loads(path.read_text(encoding="utf-8"))
    for name in ("mode", "anchor", "interval_minutes", "lead_seconds", "visible", "log_retention_days"):
        del values[name]
    path.write_text(json.dumps(values), encoding="utf-8")
    assert load_config(path) == config
    del values["progid"]
    path.write_text(json.dumps(values), encoding="utf-8")
    with pytest.raises(ValueError, match="progid"):
        load_config(path)


@pytest.mark.parametrize("text", [
    "  20261126 090507\t", "2026-11-26 09:05:07", "2026/11/26 09:05:07",
])
def test_parse_anchor(text):
    assert parse_anchor(text) == datetime(2026, 11, 26, 9, 5, 7)


@pytest.mark.parametrize("text", [
    "", "2026116 090507", "2026-11-26T09:05:07", "2026-02-30 09:05:07",
])
def test_invalid_anchor_format(text):
    with pytest.raises(ValueError, match="開始日時"):
        parse_anchor(text)


@pytest.mark.parametrize(("offset", "expected_minutes"), [
    (timedelta(microseconds=-1), 0),
    (timedelta(), 7),
    (timedelta(minutes=6, seconds=59, microseconds=999999), 7),
    (timedelta(minutes=7), 14),
])
def test_interval_next_boundaries(offset, expected_minutes):
    anchor = datetime(2026, 11, 26, 9, 0)
    assert interval_next(anchor, 7, anchor + offset) == anchor + timedelta(minutes=expected_minutes)


def test_interval_next_crosses_hour():
    anchor = datetime(2026, 11, 26, 9, 0)
    base = datetime(2026, 11, 26, 9, 49)
    scheduled = []
    for _ in range(3):
        base = interval_next(anchor, 7, base)
        scheduled.append(base.strftime("%H:%M"))
    assert scheduled == ["09:56", "10:03", "10:10"]


def test_interval_next_with_year_old_anchor():
    anchor = datetime(2025, 11, 26, 9, 0)
    base = datetime(2026, 11, 26, 9, 0)
    assert interval_next(anchor, 7, base) == datetime(2026, 11, 26, 9, 2)


def test_load_interval_config_without_cron(tmp_path):
    path = tmp_path / "config.json"
    path.write_text(json.dumps({
        "version": 1, "progid": "WaveLoggerPro2.Application", "settings_file": "settings.xxx",
        "logger_model": "NR-600", "unit_id": 0, "mode": "interval",
        "anchor": "20261126 090000", "interval_minutes": 7,
    }), encoding="utf-8")
    config = load_config(path)
    assert (config.mode, config.cron, config.anchor, config.interval_minutes) == (
        "interval", "", "20261126 090000", 7,
    )
    save_config(config, path)
    assert load_config(path) == config


@pytest.mark.parametrize(("key", "value"), [
    ("mode", "unknown"), ("anchor", None), ("anchor", 20261126), ("anchor", "invalid"),
    ("interval_minutes", None), ("interval_minutes", 0),
    ("interval_minutes", True), ("interval_minutes", 1.0),
])
def test_invalid_interval_config(config, key, value):
    config = replace(config, mode="interval", anchor="20261126 090000", interval_minutes=7)
    with pytest.raises(ValueError, match=key):
        replace(config, **{key: value})


def test_next_fire_after_uses_selected_mode(config):
    base = datetime(2026, 11, 26, 9, 0)
    cron = replace(config, anchor="invalid", interval_minutes="invalid")
    interval = replace(config, mode="interval", cron="invalid", anchor="20261126 090000", interval_minutes=7)
    assert cron.next_fire_after(base) == datetime(2026, 11, 26, 9, 1)
    assert interval.next_fire_after(base) == datetime(2026, 11, 26, 9, 7)


def test_cron_seconds_first():
    slots = cron_schedule("15,25 * * * * *", datetime(2026, 11, 26, 8, 0))
    assert [slots.get_next(datetime).strftime("%H:%M:%S") for _ in range(3)] == [
        "08:00:15", "08:00:25", "08:01:15",
    ]


@pytest.mark.parametrize(("expression", "expected"), [
    ("*/30 * * * * *", "2026-11-09T00:00:30"),
    ("0 0 9 * * *", "2026-11-09T09:00:00"),
    ("0 0 0 * * 7", "2026-11-15T00:00:00"),
    ("0 0 0 15 * 1", "2026-11-15T00:00:00"),
    ("0 0 0 */2 * 1", "2026-11-23T00:00:00"),
    ("0 0 0 1-31 * 1", "2026-11-10T00:00:00"),
    ("0 0 0 15 * */1", "2026-11-15T00:00:00"),
    ("0 0 0 31 2 1", "2027-02-01T00:00:00"),
    ("0 0 0 * * 3-3", "2026-11-11T00:00:00"),
])
def test_tb_sync_cron_examples(expression, expected):
    assert cron_schedule(expression, datetime(2026, 11, 9)).get_next(datetime).isoformat() == expected
