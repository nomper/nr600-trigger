from dataclasses import replace
from datetime import datetime, timedelta, timezone
from unittest.mock import Mock

import pytest

from src import app
from src.config import load_config


LAUNCH_TEXT = "20261126 090507"
SAVED_ANCHOR = "2026/11/25 08:04:06"


@pytest.mark.parametrize(("saved_anchor", "expected"), [
    (None, LAUNCH_TEXT),
    ("", LAUNCH_TEXT),
    (SAVED_ANCHOR, SAVED_ANCHOR),
])
def test_initial_anchor(saved_anchor, expected):
    assert app.initial_anchor(saved_anchor, LAUNCH_TEXT) == expected


class FakeStringVar:
    def __init__(self, value=""):
        self.value = value

    def get(self):
        return self.value

    def set(self, value):
        self.value = value


@pytest.fixture
def make_app(monkeypatch, config, tmp_path):
    def create(saved_anchor=None):
        launch = datetime(2026, 11, 26, 9, 5, 7, 500000, tzinfo=timezone(timedelta(hours=9)))
        clock = Mock(side_effect=[launch, launch + timedelta(seconds=1)])
        widgets = Mock()
        monkeypatch.setattr(app, "local_now", clock)
        monkeypatch.setattr(app.tk, "StringVar", FakeStringVar)
        monkeypatch.setattr(app, "ttk", widgets)
        monkeypatch.setattr(app, "load_config", Mock(return_value=replace(config, anchor=saved_anchor)))
        monkeypatch.setattr(app, "Logbook", Mock())
        monkeypatch.setattr(app, "LoggerLink", Mock())
        monkeypatch.setattr(app, "Scheduler", Mock())
        monkeypatch.setattr(app.App, "_poll", Mock())
        monkeypatch.setattr(app.App, "_poll_snapshot", Mock())

        window = app.App(Mock(), tmp_path / "config.json", tmp_path / "logs")
        cron_radio, interval_radio = widgets.Radiobutton.call_args_list
        return window, cron_radio.kwargs, interval_radio.kwargs

    return create


@pytest.mark.parametrize(("saved_anchor", "expected"), [
    (None, LAUNCH_TEXT),
    (SAVED_ANCHOR, SAVED_ANCHOR),
])
def test_app_displays_initial_anchor(make_app, saved_anchor, expected):
    window, _, _ = make_app(saved_anchor)

    assert window.anchor.get() == expected
    assert window.config.anchor == saved_anchor


@pytest.mark.parametrize(("text", "expected"), [
    ("", LAUNCH_TEXT),
    (" \t　", LAUNCH_TEXT),
    (f" {SAVED_ANCHOR} ", f" {SAVED_ANCHOR} "),
])
def test_selecting_interval_fills_only_blank_anchor(make_app, text, expected):
    window, cron_radio, interval_radio = make_app()
    window.anchor.set(text)

    assert "command" not in cron_radio
    assert interval_radio["value"] == "interval"
    interval_radio["variable"].set(interval_radio["value"])
    interval_radio["command"]()

    assert window.anchor.get() == expected


@pytest.mark.parametrize("saved_anchor", [None, SAVED_ANCHOR, f" {SAVED_ANCHOR} "])
def test_starting_cron_preserves_saved_anchor(make_app, saved_anchor):
    window, _, _ = make_app(saved_anchor)
    if saved_anchor is not None:
        window.anchor.set("20261129 103000")
    window.cron.set("15 * * * * *")

    window._start()

    saved = load_config(window.config_path)
    assert saved.mode == "cron" and saved.cron == "15 * * * * *"
    assert saved.anchor == saved_anchor
    window.scheduler.start.assert_called_once_with(saved)


def test_starting_interval_saves_anchor_field(make_app):
    window, _, _ = make_app(SAVED_ANCHOR)
    window.mode.set("interval")
    window.anchor.set(" 20261129 103000 ")
    window.interval.set("7")

    window._start()

    saved = load_config(window.config_path)
    assert (saved.mode, saved.anchor, saved.interval_minutes) == ("interval", "20261129 103000", 7)
    window.scheduler.start.assert_called_once_with(saved)


@pytest.mark.parametrize("reason", ["確立が応答待ち", "収集ソフトが応答待ち"])
def test_app_displays_com_wait_reason_even_if_link_reports_ready(make_app, reason):
    poll = app.App._poll
    window, _, _ = make_app()
    window.log = None
    window.scheduler.link.connected = True
    window.scheduler.link_wait_reason = reason
    window.scheduler.status = reason
    window.scheduler.active = False
    window.scheduler.next_at = None
    window._cached_state = "停止（1）"
    window._message = "別の操作のメッセージ"

    poll(window)

    assert window.connection.get() == window.status.get() == reason
    assert window.state.get() == "—"
