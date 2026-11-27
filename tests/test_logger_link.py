import json
from dataclasses import replace
from datetime import timedelta
from threading import Event
from time import monotonic

import pytest

from src import logger_link
from src.logbook import Logbook, local_now
from src.logger_link import (
    DISCONNECTED, FAILED, OPERATION_ERROR, SKIPPED, STATE_ERROR, FireTicket, Problem, Reading, can_start,
)
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


@pytest.mark.parametrize(("before", "after"), [(1, 4), (4, 1), (1, -1)])
def test_precheck_rereads_state_after_reopen(config, tmp_path, monkeypatch, before, after):
    log = Logbook(tmp_path)
    link = MockLink(config, log)
    open_file = _MockServer.OpenFile

    def reopen(server, path):
        retval = open_file(server, path)
        server.link.state = after
        return retval

    try:
        assert link.establish().result(timeout=1) is None
        monkeypatch.setattr(_MockServer, "OpenFile", reopen)
        link.state, link.file_type = before, 0
        observed_before = local_now()
        observed_after = observed_before + timedelta(seconds=1)
        times = iter((observed_before, observed_after))
        with monkeypatch.context() as patch:
            patch.setattr(logger_link, "local_now", lambda: next(times))
            result = link.precheck(FireTicket(observed_before), monotonic() + 1).result(timeout=1)

        if after < 0:
            assert isinstance(result, Problem)
            assert result.stage == "precheck.GetState" and result.reason == STATE_ERROR
        else:
            assert isinstance(result, Reading) and result.problem is None
            assert result.state == after and can_start(result.state) is (after == 1)
            assert result.observed_at == observed_after
        assert link.reopen_count == 1
    finally:
        link.state = 1
        assert link.close()
        log.close()

    [entry] = [
        event for path in tmp_path.glob("*.jsonl") for line in path.read_text(encoding="utf-8").splitlines()
        if (event := json.loads(line))["kind"] == "precheck"
    ]
    assert entry["reopen_success"] is True
    assert (entry["state"], entry["state_after_reopen"]) == (before, after)
    assert entry["observed_at_after_reopen"] == observed_after.isoformat(timespec="milliseconds")


@pytest.mark.parametrize("stage", ["SetIdentifier", "OpenFile", "precheck.OpenFile"])
def test_file_and_identifier_operations_reject_non_integer_return(config, tmp_path, monkeypatch, stage):
    log = Logbook(tmp_path)
    link = MockLink(config, log)
    try:
        if stage == "precheck.OpenFile":
            assert link.establish().result(timeout=1) is None
            link.file_type = 0
        monkeypatch.setattr(_MockServer, stage.rsplit(".", 1)[-1], lambda *args: False)

        if stage == "precheck.OpenFile":
            result = link.precheck(FireTicket(local_now()), monotonic() + 1).result(timeout=1)
        else:
            result = link.establish().result(timeout=1)
        assert isinstance(result, Problem)
        assert (result.stage, result.reason) == (stage, OPERATION_ERROR)
        assert "False" in result.detail and "bool" in result.detail
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


@pytest.mark.parametrize(("value", "hresult"), [
    (None, None), (0, None), (4, None), (None, -2147417848),
], ids=["type-none", "type-zero", "type-out-of-range", "read-error"])
def test_establish_without_file_type_baseline(config, tmp_path, monkeypatch, value, hresult):
    log = Logbook(tmp_path)
    link = MockLink(config, log)
    reads = []

    class ReadError(Exception):
        pass

    def read_file_type(server):
        reads.append(server.link.file_type)
        if hresult is None:
            return value
        error = ReadError("GetActiveFileType failed")
        error.hresult = hresult
        raise error

    try:
        monkeypatch.setattr(_MockServer, "GetActiveFileType", read_file_type)
        problem = link.establish().result(timeout=1)
        assert isinstance(problem, Problem)
        assert problem.stage == "establish.GetActiveFileType"
        assert problem.reason == (STATE_ERROR if hresult is None else DISCONNECTED)
        assert problem.hresult == hresult
        assert reads == [2]
        assert not link.connected
        assert link.precheck(FireTicket(local_now()), monotonic() + 1) is None
    finally:
        assert link.close()
        log.close()

    entries = [
        json.loads(line)
        for path in tmp_path.glob("*.jsonl")
        for line in path.read_text(encoding="utf-8").splitlines()
    ]
    [established] = [event for event in entries if event["kind"] == "establish"]
    assert not established["success"] and established["file_type_baseline"] is None
    assert established["failed_stage"] == problem.stage
    assert established["detail"] == problem.detail
    if value is not None:
        assert established["api_returns"]["GetActiveFileType"] == value
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


@pytest.mark.parametrize("previous_state", [-1, RuntimeError("GetState failed"), 4, 1])
def test_establish_recreates_server_only_when_previous_state_read_fails(config, tmp_path, monkeypatch, previous_state):
    log = Logbook(tmp_path)
    link = MockLink(config, log)
    servers = []
    previous_reads = [previous_state]
    recreation_needed = isinstance(previous_state, Exception) or previous_state < 0

    class PreviousServer(_MockServer):
        def GetState(self):
            if self.established and previous_reads:
                value = previous_reads.pop()
                if isinstance(value, Exception):
                    raise value
                return value
            return super().GetState()

    def dispatch():
        server = _MockServer(link) if servers else PreviousServer(link)
        servers.append(server)
        return server

    try:
        monkeypatch.setattr(link, "_dispatch", dispatch)
        assert link.establish().result(timeout=1) is None

        result = link.establish().result(timeout=1)

        if previous_state == 4:
            assert isinstance(result, Problem) and result.detail == "GetState=4"
        else:
            assert result is None
        assert len(servers) == (2 if recreation_needed else 1)
        assert link.connected and link.quit_count == 0
    finally:
        assert link.close()
        log.close()

    entries = [
        event for path in tmp_path.glob("*.jsonl") for line in path.read_text(encoding="utf-8").splitlines()
        if (event := json.loads(line))["kind"] == "establish"
    ]
    returns = entries[-1]["api_returns"]
    assert returns.get("recreate_after_state_error", False) is recreation_needed
    if recreation_needed:
        assert returns["previous_GetState_error"]["error_kind"] == STATE_ERROR


@pytest.mark.parametrize(("progid", "model", "file_type", "baseline"), [
    ("WaveLoggerXDemo.Application", "NR-500", 1, 1),
    ("WaveLoggerXDemo.Application", "NR-X100", 2, 2),
    ("WaveLoggerXDemo.Application", "NR-X100", 3, None),
    ("  waveloggerxdemo.application  ", "NR-X100", 3, None),
])
def test_establish_x_file_type_baseline(config, tmp_path, progid, model, file_type, baseline):
    config = replace(
        config, progid=progid, logger_model=model,
        settings_file="condition.ycf" if model == "NR-X100" else "condition.xcf",
    )
    log = Logbook(tmp_path)
    link = MockLink(config, log, settings_file_type=file_type)

    try:
        result = link.establish().result(timeout=1)
        if baseline is None:
            assert isinstance(result, Problem)
            assert result.stage == "establish.GetActiveFileType"
            assert result.reason == STATE_ERROR
            assert result.detail == "GetActiveFileType=3"
            assert not link.connected
        else:
            assert result is None
            assert link.connected
    finally:
        assert link.close()
        log.close()

    [established] = [
        event
        for path in tmp_path.glob("*.jsonl")
        for line in path.read_text(encoding="utf-8").splitlines()
        if (event := json.loads(line))["kind"] == "establish"
    ]
    assert established["success"] is (baseline is not None)
    assert established["file_type_baseline"] == baseline


@pytest.mark.parametrize(("progid", "model", "args", "unit_info"), [
    ("WaveLoggerXDemo.Application", "NR-X100", (0, 0), 198),
    ("WaveLoggerPro2Demo.Application", "NR-600", (0,), 194),
], ids=["X", "PRO"])
def test_snapshot_get_unit_info(config, tmp_path, monkeypatch, progid, model, args, unit_info):
    config = replace(config, progid=progid, logger_model=model)
    log = Logbook(tmp_path)
    link = MockLink(config, log)
    calls = []
    get_unit_info = _MockServer.GetUnitInfo

    def record_get_unit_info(server, *call_args):
        calls.append(call_args)
        return get_unit_info(server, *call_args)

    monkeypatch.setattr(_MockServer, "GetUnitInfo", record_get_unit_info)

    try:
        assert link.establish().result(timeout=1) is None
        snapshot = link.snapshot().result(timeout=1)

        assert calls == [args]
        assert snapshot["unit_info"] == unit_info
        assert "diagnostic_errors" not in snapshot
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


@pytest.mark.parametrize("retval", [False, 0.0, None])
def test_start_rejects_non_integer_return(config, tmp_path, monkeypatch, retval):
    log = Logbook(tmp_path)
    link = MockLink(config, log)
    try:
        assert link.establish().result(timeout=1) is None
        monkeypatch.setattr(_MockServer, "Start", lambda server: retval)

        result = link.fire(FireTicket(local_now()), monotonic() + 1).result(timeout=1)

        assert result.result == FAILED
        assert result.problem.stage == "Start"
        assert repr(retval) in result.problem.detail and type(retval).__name__ in result.problem.detail
    finally:
        assert link.close()
        log.close()
