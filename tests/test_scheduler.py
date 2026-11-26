import json
from dataclasses import replace
from datetime import datetime, timedelta
from time import monotonic, sleep

import pytest

from src.config import MockConfig
from src.logbook import Logbook
from src.logger_link import CONFIRMED, DISCONNECTED, FAILED, SKIPPED, UNCONFIRMED, UNKNOWN, Timing
from src.mock_link import MockLink
from src.scheduler import Scheduler


class Clock:
    def __init__(self, base):
        self.base = base.astimezone()
        self.started = monotonic()
        self.offset = 0.0

    def __call__(self):
        return self.base + timedelta(seconds=monotonic() - self.started + self.offset)

    def jump(self, seconds):
        self.offset += seconds


def wait_for(predicate, timeout=2.5):
    deadline = monotonic() + timeout
    while not predicate():
        assert monotonic() < deadline, "timed out waiting for application state"
        sleep(0.005)


@pytest.fixture
def rig(config, tmp_path):
    resources = []

    def create(scenario="normal", *, cron=None, seconds_to_grid=0.45, delay=0.5):
        active = replace(config, mock=MockConfig(scenario, delay), cron=cron or config.cron)
        log = Logbook(tmp_path)
        link = MockLink(active, log, timing=Timing(0.10, 0.15, 0.02, 0.20))
        clock = Clock(datetime(2026, 11, 26, 8) - timedelta(seconds=seconds_to_grid))
        scheduler = Scheduler(active, link, log, clock=clock)
        resources.append((scheduler, link, log))
        return scheduler, link, log, clock

    yield create
    for scheduler, link, log in resources:
        scheduler.stop()
        link.resume_start.set()
        wait_for(lambda: not link.busy)
        link.scenario = MockConfig()
        link.state = 1
        assert scheduler.close()
        log.close()


def events(log, kind=None):
    log.flush()
    entries = [
        json.loads(line)
        for path in log.directory.glob("*.jsonl")
        for line in path.read_text(encoding="utf-8").splitlines()
    ]
    return [event for event in entries if kind is None or event["kind"] == kind]


@pytest.mark.parametrize(("scenario", "result", "starts"), [
    ("normal", CONFIRMED, 1),
    ("busy", SKIPPED, 0),
    ("unconfirmed", UNCONFIRMED, 1),
    ("no_response", UNKNOWN, 1),
    ("start_failure", FAILED, 1),
    ("disconnect", SKIPPED, 0),
    ("disconnect_on_start", UNKNOWN, 1),
    ("disconnect_after_start", UNCONFIRMED, 1),
    ("error_stop", UNCONFIRMED, 1),
    ("delay", SKIPPED, 0),
])
def test_scheduled_result(rig, scenario, result, starts):
    scheduler, link, log, clock = rig(scenario)
    assert scheduler.start()
    event = log.results.get(timeout=2.5)
    assert event["result"] == result
    assert event["fire_id"] and event["reason"]
    assert event["scheduled_at"].strftime("%H:%M:%S") == "08:00:00"
    assert link.start_count == starts
    if scenario == "busy":
        assert "GetState=4" in event["reason"]
    if scenario == "normal":
        assert events(log, "precheck")
        assert events(log, "observe")[0]["state"] == 4
        assert 0 <= (clock() - event["scheduled_at"]).total_seconds() < 0.15
    if scenario == "start_failure":
        assert events(log, "fire")[0]["start_retval"] == -3
        assert event["error_code"] is None
    if scenario == "error_stop":
        assert event["error_code"] == 42
    if scenario == "disconnect":
        assert event["reason"] == DISCONNECTED
        wait_for(lambda: link.connected)
        assert len(events(log, "establish")) == 2
        assert events(log, "link_up")[-1]["down_ms"] > 0


@pytest.mark.parametrize(("initial", "final", "result"), [(4, 1, CONFIRMED), (1, 10, SKIPPED), (1, 2, SKIPPED)])
def test_final_state_decides(rig, initial, final, result):
    scheduler, link, log, _ = rig(seconds_to_grid=0.65)
    assert link.establish().result(timeout=1) is None
    assert scheduler.start()
    wait_for(lambda: events(log, "precheck"))
    link.state = initial
    wait_for(lambda: events(log, "precheck")[-1]["state"] == initial)
    link.state = final
    event = log.results.get(timeout=2)
    assert event["result"] == result
    assert events(log, "precheck")[-1]["state"] == final


def test_timed_out_start_holds_exclusion_and_logs_late_reply(rig):
    scheduler, link, log, _ = rig("no_response", cron="* * * * * *")
    assert scheduler.start()
    first = log.results.get(timeout=2)
    assert first["result"] == UNKNOWN
    second = log.results.get(timeout=2)
    assert second["result"] == SKIPPED
    assert link.start_count == 1
    assert link.establish() is None
    assert scheduler.snapshot() is None
    assert not scheduler.close()
    assert scheduler.status == "終了保留"
    assert link.quit_count == 0
    link.resume_start.set()
    wait_for(lambda: any(entry.get("late") for entry in events(log, "fire")))
    late = [entry for entry in events(log, "fire") if entry["late"]]
    assert [entry["fire_id"] for entry in late] == [first["fire_id"]]
    assert late[0]["start_retval"] == 0
    assert [e["result"] for e in events(log, "result") if e["fire_id"] == first["fire_id"]] == [UNKNOWN]


def test_clock_rewind_does_not_fire_same_grid_twice(rig):
    scheduler, link, log, clock = rig()
    assert scheduler.start()
    first = log.results.get(timeout=2)
    assert first["result"] == CONFIRMED
    assert scheduler.stop_collect().result(timeout=1) is None
    clock.jump(-0.5)
    wait_for(lambda: clock() > first["scheduled_at"] + timedelta(seconds=0.2))
    assert link.start_count == 1
    assert [entry["fire_id"] for entry in events(log, "result")] == [first["fire_id"]]


def test_missed_grids_are_logged_after_clock_jump(rig):
    scheduler, link, log, clock = rig(cron="* * * * * *")
    assert scheduler.start()
    wait_for(lambda: events(log, "precheck"))
    clock.jump(2.1)
    rows = [log.results.get(timeout=1) for _ in range(2)]
    assert all(event["result"] == SKIPPED for event in rows)
    assert rows[1]["scheduled_at"] - rows[0]["scheduled_at"] == timedelta(seconds=1)
    assert len({event["fire_id"] for event in rows}) == 2
    assert link.start_count == 0


def test_stop_invalidates_prechecked_grid(rig):
    scheduler, link, log, clock = rig()
    assert scheduler.start()
    wait_for(lambda: events(log, "precheck"))
    assert scheduler.snapshot() is None
    scheduler.stop()
    event = log.results.get(timeout=1)
    assert event["result"] == SKIPPED
    assert "取消" in event["reason"]
    wait_for(lambda: clock() >= event["scheduled_at"] + timedelta(seconds=0.1))
    assert link.start_count == 0
    assert scheduler.snapshot().result(timeout=1)["state"] == 1


def test_lead_starts_precheck_and_suppresses_snapshot(rig):
    scheduler, link, log, clock = rig(seconds_to_grid=8)
    assert scheduler.start()
    wait_for(lambda: link.connected)
    assert not events(log, "precheck")
    assert scheduler.snapshot().result(timeout=1)["state"] == 1
    clock.jump(3.15)
    wait_for(lambda: events(log, "precheck"))
    assert 4 < (scheduler.next_at - clock()).total_seconds() <= 5
    assert scheduler.snapshot() is None
    assert link.snapshot() is None


def test_send_now_uses_same_state_gate(rig):
    scheduler, link, log, _ = rig()
    assert link.establish().result(timeout=1) is None
    link.state = 10
    assert not scheduler.close()
    assert link.quit_count == 0
    blocked_id = scheduler.send_now()
    blocked = log.results.get(timeout=1)
    assert blocked["fire_id"] == blocked_id
    assert blocked["result"] == SKIPPED
    assert scheduler.stop_collect().result(timeout=1) is None
    sent_id = scheduler.send_now()
    sent = log.results.get(timeout=1)
    assert sent["fire_id"] == sent_id
    assert sent["result"] == CONFIRMED
    assert link.start_count == 1
