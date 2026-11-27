"""Own the automation server and classify the outcome of each Start."""

import sys
from concurrent.futures import Future, TimeoutError
from dataclasses import dataclass, field, replace
from datetime import datetime
from queue import Empty, Queue
from threading import Event, Lock, RLock, Thread
from time import monotonic, sleep
from typing import Callable
from uuid import uuid4

from .config import Config
from .logbook import Logbook, local_now


COLLECTING = frozenset({3, 4, 5, 6, 7, 12})
ACTIVE_STATES = COLLECTING | {10}
STATE_NAMES = {
    1: "停止", 2: "開始待ち", 3: "トリガ待ち", 4: "収集中", 5: "収集中",
    6: "プリトリガ", 7: "ポストトリガ", 8: "未定義", 9: "ゼロ点調整中",
    10: "読み出し中", 11: "エラー停止", 12: "収集中",
}

SKIPPED = "見送り"
UNKNOWN = "結果不明"
FAILED = "開始失敗"
CONFIRMED = "開始確認済み"
UNCONFIRMED = "送信済み・未確認"

STATE_ERROR = "状態取得異常"
OPERATION_ERROR = "操作エラー"
DISCONNECTED = "サーバー切断"
DISCONNECT_CODES = {
    0x80010108,  # RPC_E_DISCONNECTED
    0x80010007,  # RPC_E_SERVER_DIED
    0x80010012,  # RPC_E_SERVER_DIED_DNE
    0x800706BA,  # RPC_S_SERVER_UNAVAILABLE
    0x800706BE,  # RPC_S_CALL_FAILED
    0x800401FD,  # CO_E_OBJNOTCONNECTED
}


def can_start(state: int | None) -> bool:
    return type(state) is int and state == 1


@dataclass(frozen=True)
class Problem:
    stage: str
    reason: str
    detail: str
    hresult: int | None = None

    def fields(self):
        return {
            "stage": self.stage, "error_kind": self.reason,
            "detail": self.detail, "hresult": self.hresult,
        }


def _exception(stage: str, error: Exception, kind: str = OPERATION_ERROR) -> Problem:
    hresult = getattr(error, "hresult", None)
    extra = getattr(error, "excepinfo", None)
    codes = (hresult, extra[-1] if extra else None)
    if any(isinstance(code, int) and code & 0xFFFFFFFF in DISCONNECT_CODES for code in codes):
        kind = DISCONNECTED
    return Problem(stage, kind, str(error), hresult)


@dataclass(frozen=True)
class Reading:
    state: int | None
    observed_at: datetime
    finished: float
    problem: Problem | None = None

    def fields(self):
        return {
            "state": self.state, "observed_at": self.observed_at,
            **(self.problem.fields() if self.problem else {}),
        }


@dataclass(frozen=True)
class FireResult:
    result: str
    reason: str
    error_code: int | None = None
    problem: Problem | None = None


@dataclass(frozen=True)
class Timing:
    start_timeout: float = 1.0
    observe_timeout: float = 3.0
    observe_interval: float = 0.25
    close_timeout: float = 5.0
    request_timeout: float = 30.0


@dataclass(frozen=True)
class StaRequest:
    stage: str
    started: float


@dataclass
class FireTicket:
    scheduled_at: datetime
    fire_id: str = field(default_factory=lambda: uuid4().hex)
    lock: Lock = field(default_factory=Lock, repr=False)
    cancelled: bool = False
    expired: bool = False
    started: float | None = None
    started_at: datetime | None = None

    def cancel(self):
        with self.lock:
            self.cancelled = True

    def fields(self):
        return {"fire_id": self.fire_id, "scheduled_at": self.scheduled_at}


class _Worker:
    """Accept at most one queued or executing request."""

    def __init__(self, release: Callable[[], None]):
        self._release = release
        self._queue = Queue()
        self._lock = Lock()
        self._pending = False
        self._current_request = None
        self._closing = False
        self._done = Event()
        self.problem = None
        self._wake = Event()
        self._thread = Thread(target=self._run, name="logger-owner", daemon=True)

    def start(self):
        self._thread.start()

    @property
    def busy(self):
        with self._lock:
            return self._pending

    @property
    def current_request(self) -> StaRequest | None:
        with self._lock:
            return self._current_request

    @property
    def terminated(self):
        return self._done.is_set()

    def submit(self, stage, action):
        with self._lock:
            if self._pending or self._closing or self._done.is_set():
                return None
            future = Future()
            self._pending = True
            self._queue.put((stage, action, future))
            self._signal()
            return future

    def _signal(self):
        self._wake.set()

    def _request(self):
        try:
            stage, action, future = self._queue.get_nowait()
        except Empty:
            return
        with self._lock:
            self._current_request = StaRequest(stage, monotonic())
        try:
            result = action()
        except Exception as error:
            result = _exception(stage, error)
        finally:
            with self._lock:
                self._current_request = None
                self._pending = False
        future.set_result(result)

    def _finish(self, problem):
        self._release()
        with self._lock:
            self.problem = problem
            self._done.set()
            self._pending = False
            try:
                _, _, future = self._queue.get_nowait()
            except Empty:
                return
        future.set_result(problem)

    def _run(self):
        try:
            while not self._closing:
                self._wake.wait()
                self._wake.clear()
                self._request()
        finally:
            self._finish(Problem("worker", DISCONNECTED, "ワーカー終了"))

    def close(self, timeout):
        with self._lock:
            self._closing = True
            self._signal()
        self._thread.join(max(0, timeout))
        return not self._thread.is_alive()


class _StaWorker(_Worker):
    def __init__(self, release):
        # Import on demand on Windows, before creating the dedicated apartment.
        import pythoncom
        import win32event

        super().__init__(release)
        self._com = pythoncom
        self._win = win32event
        self._incoming = win32event.CreateEvent(None, False, False, None)
        self._exit = win32event.CreateEvent(None, True, False, None)

    def _signal(self):
        self._win.SetEvent(self._exit if self._closing else self._incoming)

    def _run(self):
        com, win = self._com, self._win
        initialized = False
        problem = Problem("STA", DISCONNECTED, "STA 終了")
        try:
            com.CoInitializeEx(com.COINIT_APARTMENTTHREADED)
            initialized = True
            handles = (self._exit, self._incoming)
            while True:
                ready = win.MsgWaitForMultipleObjects(handles, False, win.INFINITE, win.QS_ALLINPUT)
                index = ready - win.WAIT_OBJECT_0
                if index == 0:
                    break
                if index == 1:
                    self._request()
                if com.PumpWaitingMessages():
                    break
        except Exception as error:
            problem = _exception("STA", error)
        finally:
            self._finish(problem)
            if initialized:
                com.CoUninitialize()


class _ComObject:
    """Present the server's property-style members as zero-argument callables.

    The WAVE LOGGER automation server exposes its no-argument members
    (GetState, GetActiveFileType, GetErrorCode, GetModuleName, GetIdentifier, and Start) as
    COM properties: the manual writes them without parentheses
    (``Long = object.GetState``) and pywin32 returns their values on
    attribute access instead of a method object.  Verified on tanaka3
    against the trial server (NRWLPROS 4.02), 2026-11-26.  Members that
    take arguments (SetIdentifier, OpenFile, GetUnitInfo, ...) are real
    methods and pass through unchanged.  Each attribute access evaluates
    a property exactly once, so ``proxy.Start()`` performs one start.
    """

    def __init__(self, target):
        object.__setattr__(self, "_target", target)

    def __getattr__(self, name):
        member = getattr(object.__getattribute__(self, "_target"), name)
        if callable(member):
            return member

        def read(*args):
            if args:
                raise TypeError(f"{name} は引数を取らないプロパティです")
            return member

        return read

    def __setattr__(self, name, value):
        setattr(object.__getattribute__(self, "_target"), name, value)


@dataclass(frozen=True)
class _StartReply:
    returned_at: datetime
    returned: float
    retval: int | None = None
    problem: Problem | None = None


class LoggerLink:
    """Asynchronous operations return a Future, or None when the worker is busy."""

    def __init__(self, config: Config, log: Logbook, *, timing: Timing = Timing()):
        self.config = config
        self.log = log
        self.timing = timing
        self._gate = RLock()
        self._server = None  # Accessed only on the owner thread.
        self._file_type_baseline: int | None = None  # Accessed only on the owner thread.
        self._worker = None
        self._ready = False
        self._failure = None
        self._down_since = None
        self._protect = False
        self._firing = False
        self._closing = False
        self._closed = False

    @property
    def connected(self):
        with self._gate:
            return self._ready

    @property
    def busy(self):
        with self._gate:
            return self._firing or (self._worker is not None and self._worker.busy)

    @property
    def current_request(self) -> StaRequest | None:
        """Describe the executing STA request, if any."""
        with self._gate:
            return self._worker.current_request if self._worker is not None else None

    def protect_fire_path(self, active):
        with self._gate:
            self._protect = active

    def _new_worker(self):
        if sys.platform != "win32":
            raise RuntimeError("COM は Windows で利用できます。模擬実行には mock を設定してください")
        return _StaWorker(self._release)

    def _dispatch(self):
        from win32com.client import DispatchEx

        return _ComObject(DispatchEx(self.config.progid))

    def _release(self):
        self._server = None
        self._file_type_baseline = None
        with self._gate:
            self._ready = False

    def _link_down(self, problem):
        self._file_type_baseline = None
        if problem.reason == DISCONNECTED:
            self._server = None
        with self._gate:
            self._ready = False
            if self._failure != problem.reason:
                self._failure = problem.reason
                if self._down_since is None:
                    self._down_since = monotonic()
                self.log.emit("link_down", reason=problem.reason, **problem.fields())

    def _link_up(self):
        with self._gate:
            duration = None
            if self._down_since is not None:
                duration = (monotonic() - self._down_since) * 1000
            self.log.emit("link_up", reason=self._failure or "確立", down_ms=duration)
            self._ready = True
            self._failure = None
            self._down_since = None

    def _submit(self, stage, action):
        if self._worker is None:
            return None
        return self._worker.submit(stage, action)

    def establish(self):
        with self._gate:
            if self._closed or self._closing or self._firing:
                return None
            if self._worker is None:
                try:
                    self._worker = self._new_worker()
                    self._worker.start()
                except Exception as error:
                    self._worker = None
                    future = Future()
                    problem = _exception("STA", error)
                    self._log_establish(monotonic(), {}, problem)
                    future.set_result(problem)
                    return future
            future = self._submit("establish", self._establish)
            if future is None and self._worker.terminated:
                future = Future()
                problem = self._worker.problem
                self._worker = None
                self._log_establish(monotonic(), {}, problem)
                future.set_result(problem)
            return future

    def _establish(self):
        started = monotonic()
        values = {}
        stage = "GetState"
        problem = None
        created = False
        try:
            if self._server is not None:
                existing = self._read_state("establish.GetState")
                values["previous_GetState"] = existing.state
                if self.connected:
                    if can_start(existing.state):
                        return None
                    problem = Problem(stage, OPERATION_ERROR, f"GetState={existing.state}")
                    return problem
                if self._server is not None:
                    if existing.problem or existing.state in ACTIVE_STATES:
                        problem = existing.problem or Problem(stage, OPERATION_ERROR, "前の収集が完了していません")
                        return problem
                    stage = "Quit"
                    values["previous_Quit"] = self._server.Quit()
                    self._release()
            stage = "DispatchEx"
            self._server = self._dispatch()
            created = True
            stage = "Initialize"
            values[stage] = self._server.Initialize()
            stage = "DisplayAlerts"
            self._server.DisplayAlerts = False
            values[stage] = False
            stage = "Visible"
            self._server.Visible = self.config.visible
            values[stage] = self.config.visible
            stage = "SetIdentifier"
            values[stage] = self._server.SetIdentifier(self.config.identifier)
            if values[stage] != 0:
                problem = Problem(stage, OPERATION_ERROR, "他の WAVE LOGGER が使用中")
                return problem
            stage = "GetIdentifier"
            values[stage] = self._server.GetIdentifier()
            if values[stage] != self.config.expected_identifier:
                problem = Problem(stage, OPERATION_ERROR, "機器 ID が一致しません")
                return problem
            stage = "OpenFile"
            values[stage] = self._server.OpenFile(self.config.settings_file)
            if values[stage] != 0:
                problem = Problem(stage, OPERATION_ERROR, "OpenFile が失敗しました")
                return problem
            stage = "establish.GetActiveFileType"
            file_type = self._read_file_type(stage)
            if isinstance(file_type, Problem):
                values["GetActiveFileType_error"] = file_type.fields()
                problem = file_type
                if problem.reason == DISCONNECTED:
                    self._link_down(problem)
                return problem
            values["GetActiveFileType"] = file_type
            if file_type not in (1, 2, 3):
                problem = Problem(stage, STATE_ERROR, f"GetActiveFileType={file_type}")
                return problem
            stage = "GetState"
            reading = self._read_state("establish.GetState")
            values[stage] = reading.state
            if not can_start(reading.state):
                problem = reading.problem or Problem(stage, OPERATION_ERROR, f"GetState={reading.state}")
                return problem
            self._file_type_baseline = file_type
            self._link_up()
            return None
        except Exception as error:
            problem = _exception(stage, error)
            if problem.reason == DISCONNECTED:
                self._link_down(problem)
            return problem
        finally:
            if problem is not None and created and self._server is not None:
                try:
                    values["cleanup_Quit"] = self._server.Quit()
                except Exception as error:
                    values["cleanup_error"] = _exception("Quit", error).fields()
                finally:
                    self._release()
            self._log_establish(started, values, problem, file_type_baseline=self._file_type_baseline)

    def _log_establish(self, started, values, problem, *, file_type_baseline=None):
        self.log.emit(
            "establish", progid=self.config.progid, settings_file=self.config.settings_file,
            unit_id=self.config.unit_id, identifier=self.config.identifier,
            expected_identifier=self.config.expected_identifier,
            confirmed_identifier=values.get("GetIdentifier"), success=problem is None,
            failed_stage=problem.stage if problem else None, api_returns=values,
            file_type_baseline=file_type_baseline,
            elapsed_ms=(monotonic() - started) * 1000,
            **(problem.fields() if problem else {}),
        )

    def _read_state(self, stage):
        state = None
        problem = None
        if self._server is None:
            problem = Problem(stage, DISCONNECTED, "接続がありません")
        else:
            try:
                state = self._server.GetState()
                if type(state) is not int:
                    problem = Problem(stage, STATE_ERROR, f"GetState={state!r}")
                    state = None
                elif state < 0:
                    problem = Problem(stage, STATE_ERROR, f"GetState={state}")
            except Exception as error:
                problem = _exception(stage, error, STATE_ERROR)
        finished, observed_at = monotonic(), local_now()
        if problem:
            self._link_down(problem)
        return Reading(state, observed_at, finished, problem)

    def _read_file_type(self, stage):
        try:
            file_type = self._server.GetActiveFileType()
        except Exception as error:
            return _exception(stage, error, STATE_ERROR)
        if type(file_type) is int:
            return file_type
        return Problem(stage, STATE_ERROR, f"GetActiveFileType={file_type!r}")

    def _check_settings_file(self, ticket, deadline, fields):
        baseline = self._file_type_baseline
        fields["file_type_baseline"] = baseline
        read_stage = "precheck.GetActiveFileType"
        file_type = self._read_file_type(read_stage)
        if isinstance(file_type, Problem):
            self._link_down(file_type)
            return file_type
        fields["file_type"] = file_type
        if file_type == baseline:
            return None
        if file_type != 0:
            return Problem(
                read_stage, OPERATION_ERROR,
                f"GetActiveFileType={file_type}, file_type_baseline={baseline}",
            )

        stage = "precheck.OpenFile"
        with ticket.lock:
            if monotonic() > deadline or ticket.cancelled:
                return Problem(stage, OPERATION_ERROR, "事前確認の期限切れ・取消")
        fields["reopen_success"] = False
        try:
            retval = self._server.OpenFile(self.config.settings_file)
        except Exception as error:
            problem = _exception(stage, error)
            if problem.reason == DISCONNECTED:
                self._link_down(problem)
            return problem
        fields["open_file_retval"] = retval
        if retval != 0:
            return Problem(stage, OPERATION_ERROR, f"OpenFile={retval}")

        file_type = self._read_file_type(read_stage)
        if isinstance(file_type, Problem):
            self._link_down(file_type)
            return file_type
        fields["file_type_after_reopen"] = file_type
        if file_type != baseline:
            return Problem(
                read_stage, OPERATION_ERROR,
                f"OpenFile 後の GetActiveFileType={file_type}, file_type_baseline={baseline}",
            )
        fields["reopen_success"] = True
        return None

    def precheck(self, ticket, deadline):
        def read():
            with ticket.lock:
                if monotonic() > deadline or ticket.cancelled:
                    return Problem("precheck.GetState", OPERATION_ERROR, "事前確認の期限切れ・取消")
            reading = self._read_state("precheck.GetState")
            fields = reading.fields()
            result = reading
            if reading.problem is None and self._file_type_baseline is not None:
                problem = self._check_settings_file(ticket, deadline, fields)
                if problem is None:
                    result = replace(reading, finished=monotonic())
                else:
                    fields.update(problem.fields())
                    result = problem
            self.log.emit("precheck", **ticket.fields(), **fields)
            return result

        with self._gate:
            if self._closing or self._firing or not self._ready:
                return None
            return self._submit("precheck.GetState", read)

    def fire(self, ticket, deadline, window=None):
        with self._gate:
            if self._closing or self._firing or not self._ready:
                return None
            before = monotonic()
            dispatched_at = local_now()
            call = self._submit("Start", lambda: self._start(ticket, deadline, window))
            if call is None:
                return None
            self._firing = True
            result = Future()
            Thread(
                target=self._finish_fire,
                args=(result, call, ticket, deadline, before, dispatched_at),
                name="logger-fire-result", daemon=True,
            ).start()
            return result

    def _start(self, ticket, deadline, window):
        with ticket.lock:
            reason = None
            if ticket.cancelled:
                reason = "取消"
            elif not self.connected or self._server is None:
                reason = DISCONNECTED
            elif ticket.expired or monotonic() > deadline or (window is not None and not window()):
                reason = "Start の呼び出し期限切れ"
            if reason:
                return Problem("Start", reason, reason)
            ticket.started = monotonic()
            ticket.started_at = local_now()
        try:
            retval = self._server.Start()
        except Exception as error:
            returned, returned_at = monotonic(), local_now()
            problem = _exception("Start", error)
            if problem.reason == DISCONNECTED:
                self._link_down(problem)
            return _StartReply(returned_at, returned, problem=problem)
        return _StartReply(local_now(), monotonic(), retval=retval)

    def _finish_fire(self, result, call, ticket, deadline, before, dispatched_at):
        def log_fire(reply, late=False):
            started = ticket.started
            problem = reply.problem if isinstance(reply, _StartReply) else reply
            returned = reply if isinstance(reply, _StartReply) else None
            self.log.emit(
                "fire", **ticket.fields(), dispatched_at=dispatched_at,
                queue_wait_ms=((started if started is not None else monotonic()) - before) * 1000,
                call_started_at=ticket.started_at,
                call_returned_at=returned.returned_at if returned else None,
                call_elapsed_ms=(returned.returned - started) * 1000 if returned else None,
                start_retval=returned.retval if returned else None, late=late,
                **(problem.fields() if problem else {"hresult": None}),
            )

        try:
            reply = self._wait_start(call, ticket, deadline)
            if reply is None:
                started = ticket.started is not None
                reason = "Start の応答タイムアウト" if started else "Start のキュー待ち期限切れ"
                problem = Problem("Start", OPERATION_ERROR, reason)
                log_fire(problem)
                if started:
                    call.add_done_callback(lambda done: log_fire(done.result(), late=True))
                outcome = FireResult(UNKNOWN if started else SKIPPED, reason, problem=problem)
            else:
                log_fire(reply)
                if isinstance(reply, Problem):
                    label = UNKNOWN if ticket.started is not None else SKIPPED
                    outcome = FireResult(label, reply.detail, problem=reply)
                elif reply.problem:
                    outcome = FireResult(UNKNOWN, reply.problem.reason, problem=reply.problem)
                elif reply.retval != 0:
                    problem = Problem("Start", OPERATION_ERROR, f"Start={reply.retval}")
                    outcome = FireResult(FAILED, problem.detail, problem=problem)
                else:
                    outcome = self._observe(ticket, reply.returned)
        except Exception as error:
            problem = _exception("fire", error)
            label = UNKNOWN if ticket.started is not None else SKIPPED
            outcome = FireResult(label, problem.detail, problem=problem)
        with self._gate:
            self._firing = False
            result.set_result(outcome)

    def _wait_start(self, call, ticket, deadline):
        try:
            reply = call.result(timeout=max(0, deadline - monotonic()))
        except TimeoutError:
            with ticket.lock:
                started = ticket.started
                if started is None:
                    ticket.expired = True
                    return None
            try:
                reply = call.result(timeout=max(0, started + self.timing.start_timeout - monotonic()))
            except TimeoutError:
                return None
        if isinstance(reply, _StartReply) and reply.returned > ticket.started + self.timing.start_timeout:
            return None
        return reply

    def _observe(self, ticket, returned):
        deadline = returned + self.timing.observe_timeout
        next_poll = returned
        while monotonic() < deadline:
            sleep(max(0, next_poll - monotonic()))
            if monotonic() >= deadline:
                break
            with self._gate:
                future = self._submit(
                    "observe.GetState",
                    lambda: self._read_state("observe.GetState") if monotonic() < deadline else None,
                )
            if future is None:
                next_poll = min(deadline, monotonic() + self.timing.observe_interval)
                continue
            try:
                reading = future.result(timeout=max(0, deadline - monotonic()))
            except TimeoutError:
                break
            if reading is None:
                break
            if isinstance(reading, Problem):
                return FireResult(UNCONFIRMED, reading.reason, problem=reading)
            self.log.emit("observe", **ticket.fields(), **reading.fields())
            if reading.finished > deadline:
                break
            if reading.problem:
                return FireResult(UNCONFIRMED, reading.problem.reason, problem=reading.problem)
            if reading.state in COLLECTING:
                return FireResult(CONFIRMED, f"GetState={reading.state}")
            if reading.state == 11:
                return self._error_stop(ticket, deadline)
            next_poll = min(deadline, max(next_poll + self.timing.observe_interval, monotonic()))
        return FireResult(UNCONFIRMED, "観測期限切れ")

    def _error_stop(self, ticket, deadline):
        def diagnose():
            if self._server is None or monotonic() >= deadline:
                return None
            try:
                return self._server.GetErrorCode()
            except Exception as error:
                problem = _exception("observe.GetErrorCode", error)
                if problem.reason == DISCONNECTED:
                    self._link_down(problem)
                return problem

        with self._gate:
            future = self._submit("observe.GetErrorCode", diagnose)
        code = None
        if future is not None:
            try:
                code = future.result(timeout=max(0, deadline - monotonic()))
            except TimeoutError:
                code = Problem("observe.GetErrorCode", OPERATION_ERROR, "診断取得の期限切れ")
        if isinstance(code, Problem):
            self.log.emit(
                "observe", **ticket.fields(), state=11, observed_at=local_now(), **code.fields()
            )
            code = None
        return FireResult(UNCONFIRMED, "観測中にエラー停止（GetState=11）", code)

    def snapshot(self):
        def read():
            reading = self._read_state("snapshot.GetState")
            data = reading.fields()
            if reading.problem:
                return data
            for key, method, args in (
                ("module_name", "GetModuleName", ()), ("unit_info", "GetUnitInfo", (0,)),
            ):
                with self._gate:
                    if self._protect or self._closing:
                        break
                try:
                    data[key] = getattr(self._server, method)(*args)
                except Exception as error:
                    problem = _exception(f"snapshot.{method}", error)
                    if problem.reason == DISCONNECTED:
                        self._link_down(problem)
                        break
                    data.setdefault("diagnostic_errors", []).append(problem.fields())
            return data

        with self._gate:
            if self._protect or self._closing or not self._ready:
                return None
            return self._submit("snapshot", read)

    def stop_collect(self):
        def stop():
            try:
                value = self._server.Stop()
            except Exception as error:
                problem = _exception("Stop", error)
                if problem.reason == DISCONNECTED:
                    self._link_down(problem)
                return problem
            return value

        with self._gate:
            if self._closing or not self._ready:
                return None
            return self._submit("Stop", stop)

    def close(self):
        """Return False when the owned instance cannot be safely closed yet."""
        with self._gate:
            if self._closed:
                return True
            if self._closing:
                return False
            self._closing = True
        deadline = monotonic() + self.timing.close_timeout
        try:
            return self._close_until(deadline)
        finally:
            with self._gate:
                self._closing = False

    def _close_until(self, deadline):
        if self._worker is None or self._worker.terminated:
            self._closed = True
            return True
        while monotonic() < deadline:
            with self._gate:
                future = None if self._firing else self._submit("close", lambda: self._quit(deadline))
            if future is not None:
                try:
                    closed = future.result(timeout=max(0, deadline - monotonic()))
                except TimeoutError:
                    break
                if closed is True:
                    self._closed = self._worker.close(deadline - monotonic())
                    return self._closed
            sleep(min(self.timing.observe_interval, max(0, deadline - monotonic())))
        return False

    def _quit(self, deadline):
        if self._server is None:
            return True
        reading = self._read_state("close.GetState")
        if self._server is None:
            return True
        if reading.problem or reading.state in ACTIVE_STATES or monotonic() >= deadline:
            return False
        try:
            self._server.Quit()
        except Exception as error:
            problem = _exception("Quit", error)
            self.log.emit("manual", operation="終了", **problem.fields())
            if problem.reason != DISCONNECTED:
                return False
            self._link_down(problem)
        self._release()
        return True
