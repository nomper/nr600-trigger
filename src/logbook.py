"""Write daily JSON Lines files on a thread outside the fire path."""

import json
from datetime import date, datetime
from pathlib import Path
from queue import Queue
from threading import Lock, Thread
from uuid import uuid4


def local_now() -> datetime:
    return datetime.now().astimezone()


class Logbook:
    def __init__(self, directory: Path | str, retention_days: int = 30):
        self.directory = Path(directory)
        self.directory.mkdir(parents=True, exist_ok=True)
        self.retention_days = retention_days
        self.run_id = uuid4().hex
        self.results = Queue()
        self.errors = Queue()
        self._queue = Queue()
        self._lock = Lock()
        self._closed = False
        self.purge(local_now().date())
        self._writer = Thread(target=self._write, name="logbook", daemon=True)
        self._writer.start()

    def emit(self, kind: str, **fields) -> None:
        now = local_now()
        event = {**fields, "ts": now, "run_id": self.run_id, "kind": kind}
        with self._lock:
            if not self._closed:
                self._queue.put(event)

    def purge(self, today: date) -> None:
        for path in self.directory.glob("nr-trigger-????-??-??.jsonl"):
            try:
                day = date.fromisoformat(path.stem.removeprefix("nr-trigger-"))
            except ValueError:
                continue
            if (today - day).days >= self.retention_days and path.is_file():
                path.unlink(missing_ok=True)

    def flush(self) -> None:
        self._queue.join()

    def close(self) -> None:
        with self._lock:
            if not self._closed:
                self._closed = True
                self._queue.put(None)
        self._writer.join()

    def _write(self) -> None:
        last_day = None
        while True:
            event = self._queue.get()
            try:
                if event is None:
                    return
                day = event["ts"].date()
                if day != last_day:
                    self.purge(day)
                    last_day = day
                filename = self.directory / f"nr-trigger-{day.isoformat()}.jsonl"
                line = json.dumps(event, ensure_ascii=False, default=_encode)
                with filename.open("a", encoding="utf-8") as file:
                    file.write(line + "\n")
            except (OSError, TypeError, ValueError) as error:
                self.errors.put(str(error))
            finally:
                if event is not None and event["kind"] == "result":
                    self.results.put(event)
                self._queue.task_done()


def _encode(value):
    if isinstance(value, datetime) and value.utcoffset() is not None:
        return value.isoformat(timespec="milliseconds")
    raise TypeError(f"JSON Lines に保存できない値です: {value!r}")
