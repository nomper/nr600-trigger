import json
from dataclasses import replace
from time import monotonic

import pytest

from src.logbook import Logbook, local_now
from src.logger_link import (
    DISCONNECTED, FAILED, OPERATION_ERROR, SENT, FireResult, FireTicket, Reading, Timing, can_start,
)
from src.mock_link import MockLink, _MockServer


class _SimpleServer(_MockServer):
    def _unexpected_read(self, *args):
        raise AssertionError("シンプルモードで状態確認が呼ばれました")

    GetIdentifier = GetActiveFileType = GetState = GetModuleName = GetUnitInfo = _unexpected_read


@pytest.fixture
def link(config, tmp_path, monkeypatch):
    config = replace(
        config, link_mode="simple", progid="WaveLogger2.Application",
        logger_model="NR-500", unit_id=2, visible=True,
    )
    log = Logbook(tmp_path)
    link = MockLink(config, log, timing=Timing(observe_interval=0.01, close_timeout=0.2))
    monkeypatch.setattr(link, "_dispatch", lambda: _SimpleServer(link))
    try:
        yield link
    finally:
        closed = link.close()
        log.close()
        assert closed


def _events(link, kind):
    link.log.flush()
    return [
        event for path in link.log.directory.glob("*.jsonl")
        for line in path.read_text(encoding="utf-8").splitlines()
        if (event := json.loads(line))["kind"] == kind
    ]


def test_simple_establish_and_reuse_connected_server(link, monkeypatch):
    calls = []

    class Server(_SimpleServer):
        def Initialize(self):
            calls.append("Initialize")

        def SetIdentifier(self, identifier):
            assert self.DisplayAlerts is False
            assert self.Visible is True
            calls.append(("SetIdentifier", identifier))
            return super().SetIdentifier(identifier)

        def OpenFile(self, path):
            calls.append(("OpenFile", path))
            return super().OpenFile(path)

    def dispatch():
        calls.append("DispatchEx")
        return Server(link)

    monkeypatch.setattr(link, "_dispatch", dispatch)
    assert link.establish().result(timeout=1) is None
    assert link.connected
    assert link.establish().result(timeout=1) is None
    assert calls == [
        "DispatchEx", "Initialize", ("SetIdentifier", 2), ("OpenFile", link.config.settings_file),
    ]
    established, reused = _events(link, "establish")
    assert established["success"] and reused["success"]
    assert established["link_mode"] == reused["link_mode"] == "simple"
    assert established["confirmed_identifier"] is None
    assert established["file_type_baseline"] is None
    assert established["api_returns"].keys() == {
        "Initialize", "DisplayAlerts", "Visible", "SetIdentifier", "OpenFile",
    }
    assert reused["api_returns"] == {}


@pytest.mark.parametrize(("stage", "reason"), [
    ("SetIdentifier", "他の WAVE LOGGER が使用中"), ("OpenFile", "OpenFile が失敗しました"),
])
def test_simple_establish_handles_nonzero_returns(link, monkeypatch, stage, reason):
    monkeypatch.setattr(_SimpleServer, stage, lambda server, value: 7)

    problem = link.establish().result(timeout=1)
    assert (problem.stage, problem.reason, problem.detail) == (stage, OPERATION_ERROR, reason)
    assert not link.connected and link.quit_count == 1
    [entry] = _events(link, "establish")
    assert entry["link_mode"] == "simple" and not entry["success"]
    assert entry["api_returns"][stage] == 7


def test_simple_establish_replaces_unready_server_even_if_quit_raises(link, monkeypatch):
    calls = []

    class PreviousServer(_SimpleServer):
        def Quit(self):
            calls.append("Quit")
            raise RuntimeError("Quit failed")

    link._server = PreviousServer(link)

    def dispatch():
        assert link._server is None
        calls.append("DispatchEx")
        return _SimpleServer(link)

    monkeypatch.setattr(link, "_dispatch", dispatch)
    assert link.establish().result(timeout=1) is None
    assert link.connected
    assert calls == ["Quit", "DispatchEx"]


def test_simple_precheck_and_snapshot_do_not_submit_to_worker(link, monkeypatch):
    ticket = FireTicket(local_now())
    deadline = monotonic() + 1
    assert link.precheck(ticket, deadline) is None
    assert link.snapshot() is None
    assert link.establish().result(timeout=1) is None

    def unexpected_submit(*args):
        pytest.fail("シンプルモードの事前確認またはスナップショットが STA を使用しました")

    with monkeypatch.context() as patch:
        patch.setattr(link, "_submit", unexpected_submit)
        started = monotonic()
        precheck = link.precheck(ticket, deadline)
        assert precheck.done()
        reading = precheck.result()
        assert isinstance(reading, Reading) and can_start(reading.state) and reading.problem is None
        assert started <= reading.finished <= monotonic()
        assert reading.observed_at.utcoffset() == local_now().utcoffset()
        snapshot = link.snapshot()
        assert snapshot.done()
        assert snapshot.result() == {"state": None, "link_mode": "simple"}

    [entry] = _events(link, "precheck")
    assert entry["fire_id"] == ticket.fire_id
    assert entry["state"] is None and entry["link_mode"] == "simple"


def test_simple_fire_reports_sent_and_closes_without_reading_state(link):
    assert link.establish().result(timeout=1) is None

    result = link.fire(FireTicket(local_now()), monotonic() + 1).result(timeout=1)
    assert SENT == "送信済み" and result == FireResult(SENT, "Start=0")
    assert link.start_count == 1
    assert not _events(link, "observe")

    assert link.close()
    assert link.quit_count == 1 and not link.connected


def test_simple_fire_preserves_start_failure(link, monkeypatch):
    monkeypatch.setattr(_SimpleServer, "Start", lambda server: -3)
    assert link.establish().result(timeout=1) is None

    result = link.fire(FireTicket(local_now()), monotonic() + 1).result(timeout=1)
    assert result.result == FAILED
    assert result.reason == "Start=-3"
    assert result.problem.stage == "Start"


@pytest.mark.parametrize(("hresult", "closed", "error_kind"), [
    (None, False, OPERATION_ERROR), (-2147417848, True, DISCONNECTED),
])
def test_simple_quit_handles_exceptions(link, monkeypatch, hresult, closed, error_kind):
    def fail_quit(server):
        error = RuntimeError("Quit failed")
        error.hresult = hresult
        raise error

    assert link.establish().result(timeout=1) is None
    with monkeypatch.context() as patch:
        patch.setattr(_SimpleServer, "Quit", fail_quit)
        assert link.close() is closed
        assert link.connected is not closed
    entries = _events(link, "manual")
    assert entries and all(event["stage"] == "Quit" and event["error_kind"] == error_kind for event in entries)
