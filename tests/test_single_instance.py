import sys
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from src import app, single_instance


@pytest.mark.parametrize("last_error", [0, 183], ids=["first-instance", "duplicate"])
def test_acquire(monkeypatch, last_error):
    handle = object()
    create_mutex = Mock(return_value=handle)
    get_last_error = Mock(return_value=last_error)
    monkeypatch.setitem(sys.modules, "win32event", SimpleNamespace(CreateMutex=create_mutex))
    monkeypatch.setitem(sys.modules, "win32api", SimpleNamespace(GetLastError=get_last_error))

    assert single_instance.acquire() is (None if last_error else handle)
    create_mutex.assert_called_once_with(None, False, r"Local\NRTrigger.SingleInstance")
    get_last_error.assert_called_once_with()


def test_acquire_without_win32(monkeypatch):
    monkeypatch.setitem(sys.modules, "win32api", None)

    assert single_instance.acquire() is not None


@pytest.mark.parametrize("dialog_error", [None, OSError("dialog failed")], ids=["shown", "failed"])
def test_main_skips_initialization_for_duplicate(monkeypatch, dialog_error):
    acquire = Mock(return_value=None)
    message_box = Mock(side_effect=dialog_error)
    create_tk = Mock()
    create_app = Mock()
    monkeypatch.setattr(app.single_instance, "acquire", acquire)
    monkeypatch.setattr(
        app.ctypes, "windll", SimpleNamespace(user32=SimpleNamespace(MessageBoxW=message_box)), raising=False,
    )
    monkeypatch.setattr(app.tk, "Tk", create_tk)
    monkeypatch.setattr(app, "App", create_app)

    assert app.main([]) is None

    acquire.assert_called_once_with()
    message_box.assert_called_once_with(None, "NR Trigger はすでに起動しています。", "NR Trigger", 0x40)
    create_tk.assert_not_called()
    create_app.assert_not_called()
