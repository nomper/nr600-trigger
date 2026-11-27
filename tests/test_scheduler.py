import json
from concurrent.futures import Future
from dataclasses import replace
from datetime import datetime, timedelta
from threading import Event
from time import monotonic, sleep

import pytest

from src.config import MockConfig
from src.logbook import Logbook
from src.logger_link import (
    CONFIRMED, DISCONNECTED, FAILED, OPERATION_ERROR, SENT, SKIPPED, UNCONFIRMED, UNKNOWN, Problem, Timing,
)
from src.mock_link import MockLink, _MockServer
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

    def create(
        scenario="normal", *, grid_at=datetime(2026, 11, 26, 8), seconds_to_grid=0.45,
        delay=0.5, settings_file_type=2, **schedule,
    ):
        active = replace(config, mock=MockConfig(scenario, delay), **schedule)
        log = Logbook(tmp_path)
        link = MockLink(
            active, log, timing=Timing(0.10, 0.15, 0.02, 0.20), settings_file_type=settings_file_type,
        )
        clock = Clock(grid_at - timedelta(seconds=seconds_to_grid))
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
        [started] = events(log, "manual")
        assert started["mode"] == "cron" and started["cron"] == scheduler.config.cron
        checks = events(log, "precheck")
        assert checks and all(check["file_type"] == 2 for check in checks)
        assert link.reopen_count == 0
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


def test_simple_scheduled_result(rig):
    scheduler, link, log, _ = rig(link_mode="simple")

    assert scheduler.start()
    outcome = log.results.get(timeout=2.5)
    assert outcome["result"] == SENT and outcome["reason"] == "Start=0"
    assert link.start_count == 1
    assert not events(log, "observe")


@pytest.mark.parametrize("skip_middle", [False, True], ids=["all-fire", "skip-while-collecting"])
def test_interval_schedule_preserves_anchor(rig, skip_middle):
    scheduler, link, log, clock = rig(mode="interval", anchor="20261126 080000", interval_minutes=7)
    anchor = datetime(2026, 11, 26, 8).astimezone()
    interval = timedelta(minutes=7)

    assert scheduler.start()
    for index in range(3):
        scheduled = anchor + index * interval
        should_skip = skip_middle and index == 1
        if index > 0:
            if not should_skip:
                assert scheduler.stop_collect().result(timeout=1) is None
            clock.jump((scheduled - clock()).total_seconds() - 0.45)

        event = log.results.get(timeout=2.5)
        assert event["scheduled_at"] == scheduled
        assert event["result"] == (SKIPPED if should_skip else CONFIRMED)
        if should_skip:
            assert "GetState=4" in event["reason"]
            assert event["call_started_at"] is None
        assert scheduler.next_at == scheduled + interval

    assert link.start_count == (2 if skip_middle else 3)
    [started] = [event for event in events(log, "manual") if event["operation"] == "スケジューラ開始"]
    assert (started["mode"], started["anchor"], started["interval_minutes"]) == ("interval", "20261126 080000", 7)
    assert "cron" not in started


@pytest.mark.parametrize("baseline", [1, 3])
def test_scheduled_result_with_matching_file_type(rig, baseline):
    scheduler, link, log, _ = rig(settings_file_type=baseline)

    assert scheduler.start()
    event = log.results.get(timeout=2.5)
    assert event["result"] == CONFIRMED
    assert link.start_count == 1 and link.reopen_count == 0
    [established] = events(log, "establish")
    assert established["file_type_baseline"] == baseline
    checks = events(log, "precheck")
    assert checks and all(check["file_type"] == check["file_type_baseline"] == baseline for check in checks)


@pytest.mark.parametrize(("baseline", "reopen_fails", "result", "starts"), [
    (1, False, CONFIRMED, 1),
    (2, False, CONFIRMED, 1),
    (3, False, CONFIRMED, 1),
    (2, True, SKIPPED, 0),
])
def test_scheduled_result_after_settings_file_is_closed(rig, baseline, reopen_fails, result, starts):
    scheduler, link, log, _ = rig(settings_file_type=baseline)
    assert link.establish().result(timeout=1) is None
    link.file_type = 0
    link.reopen_fails = reopen_fails

    assert scheduler.start()
    event = log.results.get(timeout=2.5)
    assert event["result"] == result
    assert link.reopen_count == 1
    assert link.start_count == starts
    [attempt] = [check for check in events(log, "precheck") if "reopen_success" in check]
    assert attempt["fire_id"] == event["fire_id"]
    assert attempt["file_type"] == 0
    assert attempt["file_type_baseline"] == baseline
    assert attempt["reopen_success"] is (not reopen_fails)
    if reopen_fails:
        assert event["stage"] == "precheck.OpenFile"
        assert event["reason"] == OPERATION_ERROR
        assert attempt["open_file_retval"] == 1
        assert "file_type_after_reopen" not in attempt
        assert not events(log, "fire")
    else:
        assert attempt["open_file_retval"] == 0
        assert attempt["file_type_after_reopen"] == baseline


@pytest.mark.parametrize(("baseline", "active_type"), [(1, 2), (3, 1)])
def test_scheduled_result_with_different_active_file_type(rig, baseline, active_type):
    scheduler, link, log, _ = rig(settings_file_type=baseline)
    assert link.establish().result(timeout=1) is None
    link.file_type = active_type

    assert scheduler.start()
    event = log.results.get(timeout=2.5)
    assert event["result"] == SKIPPED
    assert event["stage"] == "precheck.GetActiveFileType"
    assert event["reason"] == OPERATION_ERROR
    assert link.reopen_count == 0 and link.start_count == 0
    assert link.connected and link.file_type == active_type
    [check] = events(log, "precheck")
    assert check["fire_id"] == event["fire_id"]
    assert check["file_type"] == active_type
    assert check["file_type_baseline"] == baseline


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


def test_stop_before_first_interval_grid_can_restart_at_same_time(rig):
    scheduler, link, log, clock = rig(
        seconds_to_grid=10, mode="interval", anchor="20261126 080000", interval_minutes=7,
    )
    first_at = datetime(2026, 11, 26, 8).astimezone()
    assert scheduler.start()
    assert scheduler.next_at == first_at
    wait_for(lambda: scheduler.status == "待機中")

    scheduler.stop()
    cancelled = log.results.get(timeout=1)
    assert cancelled["scheduled_at"] == first_at
    assert cancelled["result"] == SKIPPED
    assert "取消" in cancelled["reason"]
    assert link.start_count == 0

    assert scheduler.start()
    assert scheduler.next_at == first_at
    clock.jump((first_at - clock()).total_seconds() - 0.45)
    fired = log.results.get(timeout=2.5)
    assert fired["scheduled_at"] == first_at
    assert fired["result"] == CONFIRMED
    assert fired["fire_id"] != cancelled["fire_id"]
    assert scheduler.next_at == first_at + timedelta(minutes=7)
    assert link.start_count == 1


def test_initial_establish_failure_can_retry_at_same_interval_grid(rig):
    scheduler, link, log, clock = rig(
        seconds_to_grid=10, mode="interval", anchor="20261126 080000", interval_minutes=7,
    )
    first_at = datetime(2026, 11, 26, 8).astimezone()
    link.state = 10
    assert scheduler.start()
    skipped = log.results.get(timeout=1)
    assert skipped["scheduled_at"] == first_at
    assert skipped["result"] == SKIPPED
    assert skipped["reason"] == "確立失敗"
    assert not scheduler.active
    assert not link.connected
    assert link.start_count == 0

    link.state = 1
    assert scheduler.start()
    assert scheduler.next_at == first_at
    wait_for(lambda: scheduler.status == "待機中")
    clock.jump((first_at - clock()).total_seconds() - 0.45)
    fired = log.results.get(timeout=2.5)
    assert fired["scheduled_at"] == first_at
    assert fired["result"] == CONFIRMED
    assert fired["fire_id"] != skipped["fire_id"]
    assert scheduler.next_at == first_at + timedelta(minutes=7)
    assert link.start_count == 1


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


def test_establish_timeout_stops_and_discards_late_reply(rig, monkeypatch):
    scheduler, link, log, clock = rig(seconds_to_grid=8)
    link.timing = replace(link.timing, request_timeout=0.12)
    entered, release = Event(), Event()
    dispatch = link._dispatch
    establish = link.establish
    attempts = []

    def blocked_dispatch():
        entered.set()
        release.wait()
        return dispatch()

    def track_establish():
        future = establish()
        attempts.append(future)
        return future

    monkeypatch.setattr(link, "_dispatch", blocked_dispatch)
    monkeypatch.setattr(link, "establish", track_establish)
    try:
        assert scheduler.start()
        assert entered.wait(timeout=1)
        before = link.current_request
        assert before.stage == "establish" and before.started <= monotonic()
        reason = "確立が応答待ち"
        cancelled = log.results.get(timeout=2)
        assert cancelled["reason"] == reason
        assert not scheduler.active
        assert scheduler.status == scheduler.link_wait_reason == reason
        assert not attempts[0].done()

        scheduler.stop()
        assert not scheduler.start()
        assert not scheduler.active and scheduler.status == reason
        assert len(attempts) == 1 and link.current_request == before
        [error] = events(log, "error")
        assert (error["stage"], error["reason"]) == ("establish", reason)
        assert error["elapsed_ms"] > 120
    finally:
        release.set()

    assert attempts[0].result(timeout=1) is None
    wait_for(lambda: scheduler._establish is None)
    assert link.current_request is None
    clock.jump(9)
    fire_id = scheduler.send_now()
    blocked = log.results.get(timeout=1)
    assert (blocked["fire_id"], blocked["result"], blocked["reason"]) == (fire_id, SKIPPED, reason)
    assert len(attempts) == 1 and link.start_count == 0
    assert not scheduler.active and scheduler.status == reason

    assert scheduler.start()
    wait_for(lambda: scheduler.status == "待機中")
    assert len(attempts) == 2
    assert not scheduler.link_wait_reason


def test_running_request_timeout_stops_schedule(rig, monkeypatch):
    scheduler, link, log, clock = rig(seconds_to_grid=8)
    link.timing = replace(link.timing, request_timeout=0.12)
    entered, release = Event(), Event()

    def blocked_module_name(server):
        entered.set()
        release.wait()
        return "Mock WAVE LOGGER"

    monkeypatch.setattr(_MockServer, "GetModuleName", blocked_module_name)
    try:
        assert scheduler.start()
        wait_for(lambda: link.connected and not link.busy)
        assert link.current_request is None
        future = scheduler.snapshot()
        assert future is not None and entered.wait(timeout=1)
        assert link.current_request.stage == "snapshot"

        reason = "収集ソフトが応答待ち"
        result = log.results.get(timeout=2)
        assert result["reason"] == reason and result["result"] == SKIPPED
        assert not scheduler.active and scheduler.status == reason
        assert not scheduler.start()
        assert not future.done()
        [error] = events(log, "error")
        assert (error["stage"], error["reason"]) == ("snapshot", reason)
    finally:
        release.set()

    assert future.result(timeout=1)["module_name"] == "Mock WAVE LOGGER"
    assert link.current_request is None
    clock.jump(9)
    assert not scheduler.active and scheduler.status == reason
    scheduler.send_now()
    assert log.results.get(timeout=1)["reason"] == reason
    assert link.start_count == 0


def test_normal_sta_requests_do_not_trip_watchdog(rig):
    scheduler, link, log, _ = rig(seconds_to_grid=0.70)
    link.timing = replace(link.timing, request_timeout=0.20)
    assert scheduler.start()

    assert log.results.get(timeout=2)["result"] == CONFIRMED
    assert scheduler.active and scheduler.link_wait_reason is None
    assert link.current_request is None
    assert not events(log, "error")


def test_short_cron_reconnects_in_next_lead_window_even_during_backoff(rig):
    scheduler, link, log, _ = rig("disconnect", cron="* * * * * *", seconds_to_grid=0.70)
    assert scheduler.start()
    wait_for(lambda: events(log, "link_down"))
    with scheduler._guard:
        assert scheduler._next.checking and not link.connected
        scheduler._retry_at = monotonic() + 60

    first = log.results.get(timeout=2)
    assert (first["result"], first["reason"]) == (SKIPPED, DISCONNECTED)
    assert first["call_started_at"] is None

    second = log.results.get(timeout=2)
    assert second["scheduled_at"] == first["scheduled_at"] + timedelta(seconds=1)
    assert second["result"] == CONFIRMED
    assert len(events(log, "establish")) == 2
    assert link.start_count == 1
    final = [check for check in events(log, "precheck") if check["fire_id"] == second["fire_id"]][-1]
    assert final["state"] == 1
    assert datetime.fromisoformat(final["observed_at"]) <= second["call_started_at"]


def test_lead_window_protects_fire_path_while_snapshot_is_in_progress(rig, monkeypatch):
    scheduler, link, _, clock = rig(seconds_to_grid=8)
    assert scheduler.start()
    wait_for(lambda: scheduler.status == "待機中")
    entered, release = Event(), Event()
    get_state = _MockServer.GetState

    def blocked_get_state(server):
        entered.set()
        release.wait()
        return get_state(server)

    monkeypatch.setattr(_MockServer, "GetState", blocked_get_state)
    try:
        snapshot = scheduler.snapshot()
        assert snapshot is not None and entered.wait(timeout=1)
        clock.jump(4)
        wait_for(lambda: link._protect)
        with scheduler._guard:
            assert not scheduler._next.checking
    finally:
        release.set()

    value = snapshot.result(timeout=1)
    assert value["state"] == 1
    assert "module_name" not in value and "unit_info" not in value


def test_file_type_failure_is_reported_in_scheduler_status(rig):
    scheduler, link, log, _ = rig(settings_file_type=0)
    assert scheduler.start()
    result = log.results.get(timeout=2)
    assert result["reason"] == "確立失敗"
    wait_for(lambda: scheduler.status.startswith("確立失敗"))
    assert "establish.GetActiveFileType" in scheduler.status and "GetActiveFileType=0" in scheduler.status
    assert not scheduler.active and not link.connected


def test_establish_backoff_caps_and_resets_after_success(rig, monkeypatch):
    scheduler, link, _, clock = rig(seconds_to_grid=15)
    attempts = []
    problem = Problem("DispatchEx", OPERATION_ERROR, "mock failure")

    def establish():
        future = Future()
        attempts.append(future)
        return future

    monkeypatch.setattr(link, "establish", establish)

    def complete(value):
        with link._gate:
            link._ready = value is None
        attempts[-1].set_result(value)

    with scheduler._guard:
        now = 100.0
        wall = clock().replace(tzinfo=None)
        scheduler._retry_at = now + 60
        assert scheduler.start()
        scheduler._service_link(now, wall)
        assert len(attempts) == 1  # Manual start bypasses backoff.
        complete(None)
        scheduler._service_link(now, wall)

        with link._gate:
            link._ready = False
        for count, delay in enumerate((1, 2, 4, 8, 16, 32, 60, 60), start=2):
            scheduler._service_link(now, wall)
            assert len(attempts) == count
            complete(problem)
            scheduler._service_link(now, wall)
            assert scheduler._retry_at == now + delay
            scheduler._service_link(now + delay - 0.001, wall)
            assert len(attempts) == count
            now = scheduler._retry_at

        scheduler._service_link(now, wall)
        complete(None)
        scheduler._service_link(now, wall)
        assert scheduler._retry_at == 0
        with link._gate:
            link._ready = False
        scheduler._service_link(now, wall)
        complete(problem)
        scheduler._service_link(now, wall)
        assert scheduler._retry_at == now + 1
        scheduler.stop()


@pytest.mark.parametrize("schedule", [
    {"mode": "interval", "anchor": "99991211 120000", "interval_minutes": 14 * 24 * 60},
    {"mode": "cron", "cron": "0 0 12 25 12 *"},
])
def test_schedule_range_exhaustion_stops_but_collects_last_send(rig, schedule):
    last_grid = datetime(9999, 12, 25, 12)
    scheduler, link, log, _ = rig(grid_at=last_grid, **schedule)
    assert scheduler.start()
    result = log.results.get(timeout=2)
    assert result["scheduled_at"] == last_grid.astimezone()
    assert result["result"] == CONFIRMED and link.start_count == 1
    assert not scheduler.active and scheduler.next_at is None
    [error] = events(log, "error")
    assert error["stage"] == "next_fire_after"
    assert "日時の範囲外" in error["reason"]
    assert scheduler.status == error["reason"]
    assert scheduler._thread.is_alive()
    assert not scheduler.start()
    assert scheduler.status == error["reason"]
