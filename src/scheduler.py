"""Schedule one Start per local wall-clock grid time."""

from concurrent.futures import Future
from dataclasses import dataclass
from datetime import datetime, timedelta
from threading import Event, RLock, Thread
from time import monotonic

from .config import Config
from .logger_link import (
    DISCONNECTED, SKIPPED, FireResult, FireTicket, Problem, Reading, can_start,
)
from .logbook import local_now


DISPATCH_WINDOW = 0.050
FINAL_CHECK_LEAD = 0.100
PRECHECK_INTERVAL = 0.250
CLOCK_JUMP = 0.020
MANUAL_CHECK_TIMEOUT = 1.0
INITIAL_RETRY_DELAY = 1.0
MAX_RETRY_DELAY = 60.0
ESTABLISH_PENDING = "確立が応答待ち"
REQUEST_PENDING = "収集ソフトが応答待ち"
SCHEDULE_RANGE_ERROR = "次回発火時刻を計算できません（日時の範囲外）"


def _wall(value):
    return value.replace(tzinfo=None, fold=0)


def _local_grid(grid, current):
    first = grid.astimezone()
    second = grid.replace(fold=1).astimezone()
    if _wall(second) == grid and second.utcoffset() == current.utcoffset():
        return second
    # Keep the intended wall time even for a slot skipped by a DST gap.
    return grid.replace(tzinfo=first.tzinfo)


@dataclass
class _Plan:
    ticket: FireTicket
    due: float
    grid: datetime | None = None
    checking: bool = False  # The STA has accepted a precheck for this fire path.
    lead_retry_attempted: bool = False
    check: Future | None = None
    latest: Reading | None = None
    last_check: float = float("-inf")
    final_requested: bool = False
    problem: Problem | None = None


class Scheduler:
    def __init__(self, config: Config, link, log, *, clock=local_now):
        self.config = config
        self.link = link
        self.log = log
        self._clock = clock
        self._guard = RLock()
        self._wake = Event()
        self._next = None
        self._manual = None
        self._flight = None
        self._establish = None
        self._establish_started = None
        self._starting = False
        self._retry_at = 0.0
        self._retry_delay = INITIAL_RETRY_DELAY
        self.link_wait_reason = None
        self._done = set()
        self._active = False
        self._closing = False
        self._closed = False
        self._last_clock = None
        self.status = "停止中"
        self._thread = Thread(target=self._run, name="scheduler", daemon=True)
        self._thread.start()

    @property
    def next_at(self):
        with self._guard:
            return self._next.ticket.scheduled_at if self._next else None

    @property
    def active(self):
        with self._guard:
            return self._active

    def start(self, config: Config | None = None):
        with self._guard:
            if self._active or self._closing or self._closed:
                return False
            if self.link_wait_reason:
                if (self._establish is not None and not self._establish.done()) or self.link.busy:
                    self.status = self.link_wait_reason
                    return False
                # An explicit start can revalidate the link once the STA is free.
                self._establish = self._establish_started = None
            if config is not None:
                self.config = config
            wall, now = _wall(self._clock()), monotonic()
            self._last_clock = (wall, now)
            self._next = self._next_after(wall, wall, now)
            if self._next is None:
                return False
            self.link_wait_reason = None
            self._active = True
            self._starting = True
            self.status = "確立中"
            if self.config.mode == "cron":
                schedule = {"cron": self.config.cron}
            else:
                schedule = {"anchor": self.config.anchor, "interval_minutes": self.config.interval_minutes}
            self.log.emit(
                "manual", operation="スケジューラ開始",
                mode=self.config.mode, lead_seconds=self.config.lead_seconds, **schedule,
            )
            self._wake.set()
            return True

    def stop(self):
        with self._guard:
            self.log.emit("manual", operation="スケジューラ停止")
            self._stop("スケジューラ停止による取消")

    def _stop(self, reason, *, cancel_flight=True):
        self._active = False
        self._starting = False
        for plan in (self._next, self._manual):
            if plan is not None:
                remember_grid = plan.grid is not None and plan.grid <= _wall(self._clock())
                self._record(plan, FireResult(SKIPPED, reason), remember_grid=remember_grid)
        self._next = self._manual = None
        if cancel_flight and self._flight:
            self._flight[0].ticket.cancel()
        self._sync_protection()
        self.status = self.link_wait_reason or "停止中"
        self._wake.set()

    def send_now(self):
        with self._guard:
            plan = _Plan(FireTicket(self._clock()), monotonic() + MANUAL_CHECK_TIMEOUT, checking=True)
            self.log.emit("manual", operation="今すぐ送信", **plan.ticket.fields())
            wall = _wall(self._clock())
            if self._closing or self._closed:
                self._record(plan, FireResult(SKIPPED, "終了処理中"))
            elif self.link_wait_reason:
                self._record(plan, FireResult(SKIPPED, self.link_wait_reason))
            elif self._manual or self._flight or self._lead_started(wall) or self.link.busy:
                self._record(plan, FireResult(SKIPPED, "別の処理が進行中"))
            elif not self.link.connected:
                self._record(plan, FireResult(SKIPPED, DISCONNECTED))
            else:
                self._manual = plan
                self._sync_protection()
                self._wake.set()
            return plan.ticket.fire_id

    def stop_collect(self):
        future = self.link.stop_collect()

        def record(done):
            value = done.result()
            fields = value.fields() if isinstance(value, Problem) else {"api_return": value}
            self.log.emit("manual", operation="収集停止", **fields)

        if future is None:
            self.log.emit("manual", operation="収集停止", reason="未接続または処理中")
        else:
            future.add_done_callback(record)
        return future

    def snapshot(self):
        with self._guard:
            if self._closing or self.link_wait_reason or self._lead_started(_wall(self._clock())):
                return None
            return self.link.snapshot()

    def close(self):
        with self._guard:
            if self._closed:
                return True
            if self._closing:
                return False
            self._closing = True
            self._stop("終了による取消")
        closed = self.link.close()
        with self._guard:
            self._collect_flight()
            self._closed = closed
            self._closing = False
            self._wake.set()
            if not closed:
                self.status = "終了保留"
                self.log.emit("manual", operation="終了保留", reason="STA の応答または収集・読み出しの完了待ち")
        if closed:
            self._thread.join()
        return closed

    def _next_after(self, base, wall, now):
        try:
            grid = self.config.next_fire_after(base)
            in_flight = self._flight[0].grid if self._flight else None
            while grid in self._done or grid == in_flight:
                grid = self.config.next_fire_after(grid)
            ticket = FireTicket(_local_grid(grid, self._clock()))
        except (OverflowError, ValueError) as error:
            # Calendar arithmetic can also report an out-of-range year as ValueError.
            self.log.emit("error", stage="next_fire_after", reason=SCHEDULE_RANGE_ERROR, detail=str(error))
            self._stop(SCHEDULE_RANGE_ERROR, cancel_flight=False)
            self.status = SCHEDULE_RANGE_ERROR
            return None
        return _Plan(ticket, now + (grid - wall).total_seconds(), grid)

    def _lead_started(self, wall):
        return self._next is not None and (
            self._next.grid - wall
        ).total_seconds() <= self.config.lead_seconds

    def _sync_protection(self):
        self.link.protect_fire_path(bool(
            self._manual or self._flight or (self._next and self._lead_started(_wall(self._clock())))
        ))

    def _run(self):
        while True:
            self._wake.clear()
            with self._guard:
                if self._closed:
                    return
                wait = self._tick()
            self._wake.wait(wait)

    def _tick(self):
        wall, now = _wall(self._clock()), monotonic()
        self._collect_flight()
        if self._closing:
            return 0.020
        self._service_link(now, wall)
        if self._manual:
            self._tick_manual(now)
        if not self._active:
            return 0.010 if self._manual or self._flight else 0.100

        previous_wall, previous_mono = self._last_clock
        drift = (wall - previous_wall).total_seconds() - (now - previous_mono)
        self._last_clock = (wall, now)
        if drift < -CLOCK_JUMP:
            plan = self._next_after(wall, wall, now)
            if plan is None:
                return 0.020
            same_grid = (
                plan.grid == self._next.grid
                and plan.ticket.scheduled_at.utcoffset() == self._next.ticket.scheduled_at.utcoffset()
            )
            if same_grid:
                self._next.due = plan.due
                self._next.final_requested = False
                self._next.checking = self._next.checking and self._lead_started(wall)
            else:
                self._next.ticket.cancel()
                self._next = plan
            self._sync_protection()
        elif wall < self._next.grid:
            projected = now + (self._next.grid - wall).total_seconds()
            if abs(projected - self._next.due) > 0.001:
                self._next.due = projected

        # Bound each pass so thousands of skipped slots cannot block the UI.
        for _ in range(100):
            plan = self._next
            remaining = plan.due - now
            wall_remaining = (plan.grid - wall).total_seconds()
            if wall_remaining <= 0 and (drift > CLOCK_JUMP or remaining < -DISPATCH_WINDOW
                                         or wall_remaining < -DISPATCH_WINDOW):
                self._record(plan, FireResult(SKIPPED, "時計補正・復帰または発火期限切れ"))
            elif wall_remaining <= 0 and remaining <= 0:
                self._dispatch_grid(plan)
            else:
                lead_remaining = max(remaining, wall_remaining)
                if self._lead_started(wall) and not self._flight and not self._manual:
                    self._tick_precheck(plan, now, lead_remaining)
                if lead_remaining <= FINAL_CHECK_LEAD:
                    return 0.002
                if lead_remaining <= self.config.lead_seconds:
                    return min(0.020, lead_remaining - FINAL_CHECK_LEAD)
                return min(0.100, lead_remaining - self.config.lead_seconds)

            self._next = None  # This grid has already been recorded or dispatched.
            self._next = self._next_after(plan.grid, wall, now)
            if self._next is None:
                return 0.020
            self._sync_protection()
        return 0.001

    def _service_link(self, now, wall):
        timeout = self.link.timing.request_timeout
        if self._establish is not None:
            if not self._establish.done():
                if not self.link_wait_reason and now - self._establish_started > timeout:
                    request = self.link.current_request
                    self._stop_for_link_wait(
                        ESTABLISH_PENDING, request.stage if request else "establish",
                        now - self._establish_started,
                    )
                return
            future = self._establish
            self._establish = self._establish_started = None
            if self.link_wait_reason:  # Discard a reply that arrived after the timeout.
                return
            problem = future.result()
            if problem is None:
                self._retry_delay = INITIAL_RETRY_DELAY
                self._retry_at = 0.0
            else:
                self._retry_at = now + self._retry_delay
                self._retry_delay = min(self._retry_delay * 2, MAX_RETRY_DELAY)
            if self._starting:
                self._starting = False
                if problem is not None:
                    self._stop("確立失敗")
                    self.status = f"確立失敗（{problem.stage}） {problem.detail}"
                    return
                self.status = "待機中"
        if self._active or self._manual:
            request = self.link.current_request
            if request is not None and now - request.started > timeout:
                self._stop_for_link_wait(REQUEST_PENDING, request.stage, now - request.started)
                return
        if not self._active or self._flight or self._manual:
            return
        # Each grid can try once in its lead window even during backoff.
        in_lead = self._lead_started(wall)
        lead_retry = in_lead and not self._next.lead_retry_attempted
        # A lost connection stays down for the rest of the current fire path.
        if self._starting or (
            not self.link.connected and not self._next.checking and (lead_retry or now >= self._retry_at)
        ):
            self._establish = self.link.establish()
            if self._establish is not None:
                self._establish_started = now
                if in_lead:
                    self._next.lead_retry_attempted = True

    def _stop_for_link_wait(self, reason, stage, elapsed):
        self.link_wait_reason = reason
        self.log.emit("error", reason=reason, stage=stage, elapsed_ms=elapsed * 1000)
        self._stop(reason)

    def _read_check(self, plan):
        if plan.check is None or not plan.check.done():
            return
        reading = plan.check.result()
        plan.check = None
        if isinstance(reading, Problem):
            plan.problem = reading
        else:
            plan.latest = reading
            if reading.problem:
                plan.problem = reading.problem

    def _tick_precheck(self, plan, now, remaining):
        # Diagnostics yield during the lead window even while the STA is busy.
        self.link.protect_fire_path(True)
        self._read_check(plan)
        if plan.check is not None or plan.problem or plan.final_requested or not self.link.connected:
            return
        final = remaining <= FINAL_CHECK_LEAD
        if final or now - plan.last_check >= PRECHECK_INTERVAL:
            plan.check = self.link.precheck(plan.ticket, plan.due)
            if plan.check is not None:
                plan.last_check = now
                plan.final_requested = final
                plan.checking = True
                if not self._starting:
                    self.status = "事前確認中"

    def _dispatch_grid(self, plan):
        self._read_check(plan)
        # Lock the schedule to this grid even when the response is pending.
        deadline = plan.due + DISPATCH_WINDOW
        latest_wall = plan.grid + timedelta(seconds=DISPATCH_WINDOW)

        def in_window():
            return plan.grid <= _wall(self._clock()) <= latest_wall

        if self._starting or self._flight or self._manual:
            self._record(plan, FireResult(SKIPPED, "前の処理が継続中"))
        elif plan.problem:
            self._record(plan, FireResult(SKIPPED, plan.problem.reason, problem=plan.problem))
        elif not self.link.connected:
            self._record(plan, FireResult(SKIPPED, DISCONNECTED))
        elif plan.check or not plan.final_requested:
            self._record(plan, FireResult(SKIPPED, "最終状態を締切までに取得できませんでした"))
        else:
            self._dispatch(plan, deadline, in_window)

    def _tick_manual(self, now):
        plan = self._manual
        self._read_check(plan)
        if plan.problem:
            self._record(plan, FireResult(SKIPPED, plan.problem.reason, problem=plan.problem))
        elif plan.latest:
            self._dispatch(plan, now + DISPATCH_WINDOW)
        elif now >= plan.due:
            self._record(plan, FireResult(SKIPPED, "最終状態を締切までに取得できませんでした"))
        else:
            if plan.check is None:
                plan.check = self.link.precheck(plan.ticket, plan.due)
            return
        self._manual = None
        self._sync_protection()

    def _dispatch(self, plan, deadline, window=None):
        reading = plan.latest
        if reading is None or reading.finished > plan.due:
            self._record(plan, FireResult(SKIPPED, "最終状態を締切までに取得できませんでした"))
        elif not can_start(reading.state):
            self._record(plan, FireResult(SKIPPED, f"開始不可（GetState={reading.state}）"))
        else:
            result = self.link.fire(plan.ticket, deadline, window)
            if result is None:
                self._record(plan, FireResult(SKIPPED, "STA が処理中または未接続"))
            else:
                self._flight = (plan, result)
                self._sync_protection()
                self.status = "送信処理中"

    def _collect_flight(self):
        if self._flight and self._flight[1].done():
            plan, result = self._flight
            self._flight = None
            self._record(plan, result.result(), update_status=self.status != SCHEDULE_RANGE_ERROR)
            self._sync_protection()

    def _record(self, plan, outcome, *, remember_grid=True, update_status=True):
        plan.ticket.cancel()
        if remember_grid and plan.grid is not None:
            if plan.grid in self._done:
                return
            self._done.add(plan.grid)
        self.log.emit(
            "result", **plan.ticket.fields(), result=outcome.result,
            reason=outcome.reason, error_code=outcome.error_code,
            call_started_at=plan.ticket.started_at,
            **(outcome.problem.fields() if outcome.problem else {}),
        )
        if update_status and not self.link_wait_reason:
            self.status = outcome.result
