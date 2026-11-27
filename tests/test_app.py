from dataclasses import replace
from datetime import datetime, timedelta, timezone
from unittest.mock import Mock

import pytest

from src import app
from src.config import MockConfig, load_config


LAUNCH_TEXT = "20261126 090507"
SAVED_ANCHOR = "2026/11/25 08:04:06"


@pytest.mark.parametrize(("saved_anchor", "expected"), [
    (None, LAUNCH_TEXT),
    ("", LAUNCH_TEXT),
    (SAVED_ANCHOR, SAVED_ANCHOR),
])
def test_initial_anchor(saved_anchor, expected):
    assert app.initial_anchor(saved_anchor, LAUNCH_TEXT) == expected


@pytest.fixture
def connection_inputs():
    return dict(
        progid="WaveLogger2.Application", settings_file=r"C:\条件\測定.xcf",
        logger_model="NR-500", unit_id="3", link_mode="simple", visible=True,
    )


def test_connection_config_converts_dialog_inputs(config, connection_inputs):
    updated = app.connection_config(config, **connection_inputs)

    assert updated == replace(config, **dict(connection_inputs, unit_id=3))
    assert config.unit_id == 0


@pytest.mark.parametrize(("field", "value", "message"), [
    ("progid", "  ", "progid は空でない文字列を指定してください"),
    ("settings_file", "", "settings_file は空でない文字列を指定してください"),
    ("logger_model", "NR-700", "logger_model は NR-500、NR-600、NR-X100 のいずれかを指定してください"),
    ("unit_id", "4", "unit_id は 0〜3 の整数を指定してください"),
    ("unit_id", "abc", "unit_id は 0〜3 の整数を指定してください"),
    ("link_mode", "unknown", "link_mode は full または simple を指定してください"),
])
def test_connection_config_reports_validation_error(config, connection_inputs, field, value, message):
    connection_inputs[field] = value

    with pytest.raises(ValueError, match=f"^{message}$"):
        app.connection_config(config, **connection_inputs)


def test_connection_config_preserves_interval_schedule_and_other_settings(config, connection_inputs):
    current = replace(
        config, mode="interval", anchor=SAVED_ANCHOR, interval_minutes=7, lead_seconds=3,
        log_retention_days=17, mock=MockConfig(scenario="delay", delay_seconds=0.1),
    )

    updated = app.connection_config(current, **connection_inputs)

    assert updated == replace(current, **dict(connection_inputs, unit_id=3))
    assert updated.mock is current.mock


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


def test_connection_dialog_offers_nr_x100_and_ycf(make_app, monkeypatch):
    window, _, _ = make_app()
    window.scheduler.active = False
    dialog = Mock()
    settings_path = r"C:\条件\測定.ycf"
    askopenfilename = Mock(return_value=settings_path)
    monkeypatch.setattr(app.tk, "Toplevel", Mock(return_value=dialog))
    monkeypatch.setattr(app.tk, "StringVar", lambda master, value: FakeStringVar(value))
    monkeypatch.setattr(app.tk, "BooleanVar", lambda master, value: FakeStringVar(value))
    monkeypatch.setattr(app.filedialog, "askopenfilename", askopenfilename)
    app.ttk.Button.reset_mock()

    window._edit_connection()

    model_combo, _ = app.ttk.Combobox.call_args_list
    assert model_combo.kwargs["values"] == ("NR-500", "NR-600", "NR-X100")
    browse = next(call.kwargs["command"] for call in app.ttk.Button.call_args_list if call.kwargs["text"] == "参照")
    browse()
    askopenfilename.assert_called_once_with(
        parent=dialog,
        filetypes=(("収録条件ファイル", ("*.ucf", "*.xcf", "*.ycf")), ("すべてのファイル", "*")),
    )
    settings_entry = app.ttk.Entry.call_args_list[-1]
    assert settings_entry.kwargs["textvariable"].get() == settings_path


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
