import json
from threading import Event
from time import monotonic

import pytest

from src.logbook import Logbook, local_now
from src.logger_link import DISCONNECTED, FAILED, OPERATION_ERROR, SKIPPED, STATE_ERROR, FireTicket, Problem
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


def test_precheck_rejects_successful_open_without_expected_file_type(config, tmp_path, monkeypatch):
    log = Logbook(tmp_path)
    link = MockLink(config, log)
    paths = []

    class Server(_MockServer):
        def OpenFile(self, path):
            paths.append(path)
            if not self.established:
                return super().OpenFile(path)
            return 0

    try:
        monkeypatch.setattr(link, "_dispatch", lambda: Server(link))
        assert link.establish().result(timeout=1) is None
        link.file_type = 0
        ticket = FireTicket(local_now())
        problem = link.precheck(ticket, monotonic() + 1).result(timeout=1)
        assert isinstance(problem, Problem)
        assert problem.stage == "precheck.GetActiveFileType"
        assert problem.reason == OPERATION_ERROR
        assert paths == [config.settings_file, config.settings_file]
        assert link.connected
    finally:
        assert link.close()
        log.close()


@pytest.mark.parametrize(("hresult", "reason"), [(None, STATE_ERROR), (-2147417848, DISCONNECTED)])
def test_precheck_file_type_read_error(config, tmp_path, monkeypatch, hresult, reason):
    log = Logbook(tmp_path)
    link = MockLink(config, log)

    class ReadError(Exception):
        pass

    def read_error(server):
        error = ReadError("GetActiveFileType failed")
        error.hresult = hresult
        raise error

    try:
        assert link.establish().result(timeout=1) is None
        monkeypatch.setattr(_MockServer, "GetActiveFileType", read_error)
        ticket = FireTicket(local_now())
        problem = link.precheck(ticket, monotonic() + 1).result(timeout=1)
        assert isinstance(problem, Problem)
        assert problem.stage == "precheck.GetActiveFileType"
        assert problem.reason == reason
        assert problem.hresult == hresult
        assert not link.connected
        assert link.reopen_count == 0
    finally:
        assert link.close()
        log.close()


@pytest.mark.parametrize("hresult", [None, -2147417848], ids=["type-zero", "read-error"])
def test_establish_without_file_type_baseline(config, tmp_path, monkeypatch, hresult):
    log = Logbook(tmp_path)
    link = MockLink(config, log)
    reads = []

    class ReadError(Exception):
        pass

    def read_file_type(server):
        reads.append(server.link.file_type)
        if hresult is None:
            return 0
        error = ReadError("GetActiveFileType failed")
        error.hresult = hresult
        raise error

    try:
        monkeypatch.setattr(_MockServer, "GetActiveFileType", read_file_type)
        assert link.establish().result(timeout=1) is None
        link.file_type = 0
        ticket = FireTicket(local_now())
        reading = link.precheck(ticket, monotonic() + 1).result(timeout=1)
        assert reading.state == 1 and reading.problem is None
        assert reads == [2]
        assert link.connected and link.reopen_count == 0
    finally:
        assert link.close()
        log.close()

    entries = [
        json.loads(line)
        for path in tmp_path.glob("*.jsonl")
        for line in path.read_text(encoding="utf-8").splitlines()
    ]
    [established] = [event for event in entries if event["kind"] == "establish"]
    assert established["success"] and established["file_type_baseline"] is None
    if hresult is None:
        assert established["api_returns"]["GetActiveFileType"] == 0
    else:
        error = established["api_returns"]["GetActiveFileType_error"]
        assert error["stage"] == "establish.GetActiveFileType"
        assert error["hresult"] == hresult


def test_reconnect_reads_new_file_type_baseline(config, tmp_path):
    log = Logbook(tmp_path)
    link = MockLink(config, log, settings_file_type=1)

    try:
        assert link.establish().result(timeout=1) is None
        link.disconnect.set()
        ticket = FireTicket(local_now())
        reading = link.precheck(ticket, monotonic() + 1).result(timeout=1)
        assert reading.problem.reason == DISCONNECTED

        link.settings_file_type = 3
        assert link.establish().result(timeout=1) is None
        reading = link.precheck(ticket, monotonic() + 1).result(timeout=1)
        assert reading.state == 1 and reading.problem is None
        assert link.file_type == 3
        assert link.reopen_count == 0
    finally:
        assert link.close()
        log.close()


def test_precheck_does_not_read_file_type_after_state_error(config, tmp_path, monkeypatch):
    log = Logbook(tmp_path)
    link = MockLink(config, log)
    reads = []

    def read_file_type(server):
        reads.append(1)
        return 2

    try:
        monkeypatch.setattr(_MockServer, "GetActiveFileType", read_file_type)
        assert link.establish().result(timeout=1) is None
        link.state = -1
        ticket = FireTicket(local_now())
        reading = link.precheck(ticket, monotonic() + 1).result(timeout=1)
        assert reading.problem.reason == STATE_ERROR
        assert reads == [1]  # Only the baseline read during establishment.
    finally:
        link.state = 1
        assert link.close()
        log.close()


def test_file_closed_after_precheck_is_start_failure(config, tmp_path):
    log = Logbook(tmp_path)
    link = MockLink(config, log)

    try:
        assert link.establish().result(timeout=1) is None
        ticket = FireTicket(local_now())
        reading = link.precheck(ticket, monotonic() + 1).result(timeout=1)
        assert reading.state == 1 and reading.problem is None

        link.file_type = 0
        result = link.fire(ticket, monotonic() + 1).result(timeout=1)
        assert result.result == FAILED
        assert result.problem.detail == "Start=1"
        assert link.start_count == 1
        assert link.reopen_count == 0
    finally:
        assert link.close()
        log.close()
