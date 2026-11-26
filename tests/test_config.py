import json
from dataclasses import replace
from datetime import datetime

import pytest

from src.config import MockConfig, cron_schedule, load_config, save_config


@pytest.mark.parametrize(("model", "offset"), [("NR-500", 0), ("NR-600", 4)])
def test_identifier(config, model, offset):
    for unit_id in range(4):
        assert replace(config, logger_model=model, unit_id=unit_id).identifier == offset + unit_id


@pytest.mark.parametrize(("key", "value"), [
    ("version", True), ("version", 2), ("unit_id", True), ("unit_id", 4),
    ("unit_id", -1), ("logger_model", "NR-X"), ("lead_seconds", 2),
    ("lead_seconds", 6), ("lead_seconds", 3.0), ("log_retention_days", 0),
    ("visible", 1), ("progid", " "), ("settings_file", None),
    ("cron", "* * * * *"), ("cron", "60 * * * * *"),
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
    for name in ("lead_seconds", "visible", "log_retention_days"):
        del values[name]
    path.write_text(json.dumps(values), encoding="utf-8")
    assert load_config(path) == config
    del values["progid"]
    path.write_text(json.dumps(values), encoding="utf-8")
    with pytest.raises(ValueError, match="progid"):
        load_config(path)


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
