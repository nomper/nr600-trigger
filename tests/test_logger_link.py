from threading import Event
from time import monotonic

from src.logbook import Logbook, local_now
from src.logger_link import OPERATION_ERROR, SKIPPED, FireTicket
from src.mock_link import MockLink, _MockServer


def test_establish_order_and_partial_cleanup(config, tmp_path, monkeypatch):
    log = Logbook(tmp_path)
    link = MockLink(config, log)
    calls = []

    class FailingServer(_MockServer):
        def Initialize(self):
            calls.append("Initialize")

        def SetIdentifier(self, value):
            assert self.DisplayAlerts is False
            assert self.Visible is config.visible
            calls.append(("SetIdentifier", value))
            return super().SetIdentifier(value)

        def GetIdentifier(self):
            calls.append("GetIdentifier")
            return super().GetIdentifier()

        def OpenFile(self, path):
            calls.append(("OpenFile", path))
            return 7

        def GetState(self):
            calls.append("GetState")
            return super().GetState()

        def Quit(self):
            calls.append("Quit")
            super().Quit()

    original = link._dispatch

    def dispatch():
        calls.append("DispatchEx")
        return FailingServer(link)

    try:
        monkeypatch.setattr(link, "_dispatch", dispatch)
        problem = link.establish().result(timeout=1)
        assert problem.stage == "OpenFile"
        assert problem.reason == OPERATION_ERROR
        assert calls == [
            "DispatchEx", "Initialize", ("SetIdentifier", 4), "GetIdentifier",
            ("OpenFile", config.settings_file), "Quit",
        ]
        assert not link.connected
        assert link.quit_count == 1

        monkeypatch.setattr(link, "_dispatch", original)
        assert link.establish().result(timeout=1) is None
        assert link.connected
    finally:
        assert link.close()
        log.close()


def test_expired_start_request_cannot_start_later(config, tmp_path, monkeypatch):
    log = Logbook(tmp_path)
    link = MockLink(config, log)
    release = Event()
    original = link._start

    def delayed_start(*args):
        release.wait()
        return original(*args)

    try:
        assert link.establish().result(timeout=1) is None
        monkeypatch.setattr(link, "_start", delayed_start)
        ticket = FireTicket(local_now())
        result = link.fire(ticket, monotonic() + 0.05)
        assert result.result(timeout=1).result == SKIPPED
        assert link.establish() is None
    finally:
        release.set()
        assert link.close()
        log.close()
    assert link.start_count == 0
