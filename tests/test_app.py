from dataclasses import replace
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from src import app
from src.config import MockConfig, load_config, save_config


LAUNCH_TEXT = "20261126 090507"
SAVED_ANCHOR = "2026/11/25 08:04:06"
original_poll = app.App._poll


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
        self.callbacks = []

    def get(self):
        return self.value

    def set(self, value):
        self.value = value
        for callback in self.callbacks:
            callback()

    def trace_add(self, mode, callback):
        assert mode == "write"
        self.callbacks.append(callback)


@pytest.fixture
def make_app(monkeypatch, config, tmp_path):
    def create(saved_anchor=None, mock=None):
        launch = datetime(2026, 11, 26, 9, 5, 7, 500000, tzinfo=timezone(timedelta(hours=9)))
        clock = Mock(side_effect=[launch, launch + timedelta(seconds=1)])
        widgets = Mock()
        monkeypatch.setattr(app, "local_now", clock)
        monkeypatch.setattr(app.tk, "StringVar", FakeStringVar)
        monkeypatch.setattr(app, "ttk", widgets)
        monkeypatch.setattr(app, "load_config", Mock(return_value=replace(config, anchor=saved_anchor, mock=mock)))
        monkeypatch.setattr(app, "Logbook", Mock())
        monkeypatch.setattr(app, "LoggerLink", Mock())
        monkeypatch.setattr(app, "Scheduler", Mock())
        monkeypatch.setattr(app.App, "_poll", Mock())
        monkeypatch.setattr(app.App, "_poll_snapshot", Mock())

        window = app.App(Mock(), tmp_path / "config.json", tmp_path / "logs")
        window.scheduler.active = False
        window.scheduler.next_at = None
        window.scheduler.status = "停止中"
        window.scheduler.link.connected = False
        window.scheduler.link_wait_reason = None
        window.scheduler.link.timing.close_timeout = 5.0
        window.scheduler.start.return_value = True
        window.scheduler._start_outcome = True
        cron_radio, interval_radio = widgets.Radiobutton.call_args_list
        return window, cron_radio.kwargs, interval_radio.kwargs

    return create


@pytest.fixture
def connection_dialog(make_app, monkeypatch):
    window, _, _ = make_app()
    dialog = Mock()
    monkeypatch.setattr(app.tk, "Toplevel", Mock(return_value=dialog))
    monkeypatch.setattr(app.tk, "StringVar", lambda master, value: FakeStringVar(value))
    monkeypatch.setattr(app.tk, "BooleanVar", lambda master, value: FakeStringVar(value))
    controls = [Mock(), Mock()]
    app.ttk.Frame.return_value.winfo_children.return_value = controls
    app.ttk.Button.reset_mock()

    window._edit_connection()

    commands = {call.kwargs["text"]: call.kwargs["command"] for call in app.ttk.Button.call_args_list}
    return window, dialog, commands, controls


def test_connection_dialog_offers_nr_x100_and_ycf(connection_dialog, monkeypatch):
    _, dialog, commands, _ = connection_dialog
    settings_path = r"C:\条件\測定.ycf"
    askopenfilename = Mock(return_value=settings_path)
    monkeypatch.setattr(app.filedialog, "askopenfilename", askopenfilename)

    _, model_combo, _ = app.ttk.Combobox.call_args_list
    assert model_combo.kwargs["values"] == ("NR-500", "NR-600", "NR-X100")
    commands["参照"]()
    askopenfilename.assert_called_once_with(
        parent=dialog,
        filetypes=(("収録条件ファイル", ("*.ucf", "*.xcf", "*.ycf")), ("すべてのファイル", "*")),
    )
    settings_entry = app.ttk.Entry.call_args_list[-1]
    assert settings_entry.kwargs["textvariable"].get() == settings_path


def test_connection_dialog_updates_software_from_editable_progid(connection_dialog):
    progid_combo = app.ttk.Combobox.call_args_list[0].kwargs
    assert progid_combo["state"] == "normal"
    assert progid_combo["values"] == (
        "WaveLoggerPro2.Application", "WaveLoggerPro2Demo.Application",
        "WaveLoggerX.Application", "WaveLoggerXDemo.Application",
        "WaveLogger2.Application", "WaveLogger2Demo.Application",
    )
    progid = progid_combo["textvariable"]
    software = app.ttk.Label.call_args_list[-1].kwargs["textvariable"]
    assert software.get() == "PRO"
    for value, expected in (
        ("  waveloggerx.application  ", "X"), ("wavelogger2demo.application", "無印"),
        ("Custom.Application", "判定不能"),
    ):
        progid.set(value)
        assert software.get() == expected


def test_connection_save_close_exception_is_displayed_and_aborts_save(connection_dialog, monkeypatch):
    window, dialog, commands, controls = connection_dialog
    current = window.config
    save = Mock()
    showerror = Mock()
    window.scheduler.close.side_effect = RuntimeError("close exploded")
    monkeypatch.setattr(app, "Thread", lambda *, target, args, **_: SimpleNamespace(start=lambda: target(*args)))
    monkeypatch.setattr(app, "save_config", save)
    monkeypatch.setattr(app.messagebox, "showerror", showerror)

    commands["保存"]()

    assert window.config is current and window._connection_dialog is dialog
    save.assert_not_called()
    dialog.after.assert_not_called()
    showerror.assert_called_once_with("接続設定", "接続を閉じられません。close exploded", parent=dialog)
    for control in controls:
        assert [call.args[0] for call in control.state.call_args_list] == [("disabled",), ("!disabled",)]


def test_connection_save_timeout_restores_dialog_and_abandons_late_save(connection_dialog, monkeypatch):
    window, dialog, commands, controls = connection_dialog
    current, previous_scheduler = window.config, window.scheduler
    save = Mock()
    showerror = Mock()
    thread = Mock()
    monkeypatch.setattr(app, "Thread", thread)
    monkeypatch.setattr(app, "save_config", save)
    monkeypatch.setattr(app.messagebox, "showerror", showerror)
    monkeypatch.setattr(app, "time", SimpleNamespace(monotonic=Mock(side_effect=[0, 0, 8])))

    commands["保存"]()
    _, callback, *args = dialog.after.call_args.args
    callback(*args)

    dialog.after.assert_called_once()
    showerror.assert_called_once_with("接続設定", "接続を閉じる処理がタイムアウトしました。", parent=dialog)
    for control in controls:
        control.state.assert_called_with(("!disabled",))
    commands["キャンセル"]()
    dialog.destroy.assert_called_once()
    close = thread.call_args.kwargs
    previous_scheduler.close.return_value = True
    close["target"](*close["args"])
    new_scheduler = Mock(active=False, next_at=None, status="停止中", link_wait_reason=None)
    new_scheduler.link.connected = False
    window._new_scheduler = Mock(return_value=new_scheduler)
    window.log = None
    original_poll(window)

    assert window.config is current and window.scheduler is new_scheduler
    assert window._connection_close_pending is None
    window._new_scheduler.assert_called_once_with(current)
    save.assert_not_called()


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


@pytest.mark.parametrize("accepted", [False, True], ids=["rejected", "establish-failed"])
def test_failed_start_keeps_configuration(make_app, accepted):
    window, _, _ = make_app()
    prior = window.config
    save_config(prior, window.config_path)
    contents = window.config_path.read_bytes()
    window.cron.set("15 * * * * *")
    window.scheduler.start.return_value = accepted
    window.scheduler._start_outcome = None if accepted else False
    window.log = None

    window._start()
    assert window.config is prior and window.config_path.read_bytes() == contents
    assert window._message == ""
    window.scheduler.start.assert_called_once_with(replace(prior, cron="15 * * * * *"))

    window.scheduler._start_outcome = False
    window.scheduler.status = "確立失敗（GetState）"
    original_poll(window)

    assert window.status.get() == "確立失敗（GetState）"
    assert window.config is prior and window.config_path.read_bytes() == contents
    assert window._pending_start is None


def test_start_saves_only_after_async_establishment_succeeds(make_app):
    window, _, _ = make_app()
    prior = window.config
    window.scheduler._start_outcome = None
    window.cron.set("15 * * * * *")
    window.log = None

    window._start()
    assert window.config is prior and not window.config_path.exists()

    window.scheduler._start_outcome = True
    original_poll(window)

    assert window.config == load_config(window.config_path) == replace(prior, cron="15 * * * * *")


@pytest.mark.parametrize("reason", ["確立が応答待ち", "収集ソフトが応答待ち"])
def test_app_displays_com_wait_reason_even_if_link_reports_ready(make_app, reason):
    window, _, _ = make_app()
    window.log = None
    window.scheduler.link.connected = True
    window.scheduler.link_wait_reason = reason
    window.scheduler.status = reason
    window.scheduler.active = False
    window.scheduler.next_at = None
    window._cached_state = "停止（1）"
    window._message = "別の操作のメッセージ"

    original_poll(window)

    assert window.connection.get() == window.status.get() == reason
    assert window.state.get() == "—"


@pytest.mark.parametrize(("connected", "wait_reason", "label"), [
    (False, None, "未確立"), (True, None, "確立済み"),
    (True, "収集ソフトが応答待ち", "収集ソフトが応答待ち"),
])
def test_poll_labels_mock_mode_and_enables_send_only_on_connected_link(make_app, connected, wait_reason, label):
    mock = MockConfig(scenario="delay", delay_seconds=0.1)
    window, _, _ = make_app(mock=mock)
    assert window.log.emit.call_args.kwargs["app_build"] == "1.3.1"
    assert window.log.emit.call_args.kwargs["mock"] == {"scenario": "delay", "delay_seconds": 0.1}
    assert any(call.kwargs.get("text") == "設定上の収録条件・機器" for call in app.ttk.Label.call_args_list)
    window.log = None
    window.send_button = Mock()
    window.scheduler.link.connected = connected
    window.scheduler.link_wait_reason = wait_reason

    original_poll(window)

    assert window.connection.get() == f"{label}（模擬運転）"
    window.send_button.state.assert_called_once_with(("!disabled" if connected else "disabled",))


def test_deferred_exit_prompts_user_to_press_exit_again(make_app):
    window, _, _ = make_app()
    window.log = None
    window._closing = True
    window._close_results.put(False)

    original_poll(window)

    assert not window._closing
    assert "もう一度「終了」を押してください" in window.status.get()


def test_software_version_returns_none_for_unexpected_error(monkeypatch):
    winreg = Mock(KEY_WOW64_32KEY=512, KEY_WOW64_64KEY=256, KEY_READ=1)
    winreg.OpenKey.return_value.__enter__ = Mock()
    winreg.OpenKey.return_value.__exit__ = Mock(return_value=False)
    winreg.QueryValueEx.return_value = ('"C:\\logger.exe"', 1)
    version_info = Mock(side_effect=RuntimeError("invalid version resource"))
    monkeypatch.setattr(app.sys, "platform", "win32")
    monkeypatch.setitem(app.sys.modules, "winreg", winreg)
    monkeypatch.setitem(app.sys.modules, "win32api", SimpleNamespace(GetFileVersionInfo=version_info))

    assert app._software_version("WaveLoggerPro2.Application") is None
    assert version_info.call_count == 3
