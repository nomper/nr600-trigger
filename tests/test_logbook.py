import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

from src.logbook import Logbook, local_now


def test_json_lines_and_result_delivery(tmp_path):
    log = Logbook(tmp_path)
    when = datetime(2026, 11, 26, 12, 30, 0, 123456, tzinfo=timezone(timedelta(hours=9)))
    try:
        log.emit("precheck", fire_id="one", scheduled_at=when, state=10, observed_at=when)
        log.emit(
            "result", fire_id="one", scheduled_at=when,
            result="見送り", reason="読み出し中", error_code=None,
        )
        log.flush()
        path, = tmp_path.glob("*.jsonl")
        first, second = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]
        assert first["kind"] == "precheck"
        assert first["state"] == 10
        assert first["scheduled_at"] == "2026-11-26T12:30:00.123+09:00"
        assert second["kind"] == "result"
        assert second["reason"] == "読み出し中"
        for event in (first, second):
            assert event["run_id"] == log.run_id
            assert event["fire_id"] == "one"
            ts = datetime.fromisoformat(event["ts"])
            assert ts.utcoffset() is not None and ts.microsecond % 1000 == 0
        assert log.results.get_nowait()["result"] == "見送り"
        assert log.results.empty()
    finally:
        log.close()


def test_retention_only_deletes_managed_old_logs(tmp_path):
    today = local_now().date()
    for days in (0, 29, 30, 45, -1):
        day = today - timedelta(days=days)
        (tmp_path / f"nr-trigger-{day}.jsonl").touch()
    (tmp_path / "unrelated.jsonl").touch()
    (tmp_path / "nr-trigger-2026-99-99.jsonl").touch()

    log = Logbook(tmp_path, 30)
    log.close()
    assert {path.name for path in tmp_path.iterdir()} == {
        f"nr-trigger-{today - timedelta(days=days)}.jsonl" for days in (0, 29, -1)
    } | {"unrelated.jsonl", "nr-trigger-2026-99-99.jsonl"}


def test_purge_failure_does_not_prevent_other_deletions_or_log_writes(tmp_path, monkeypatch):
    today = local_now().date()
    locked = tmp_path / f"nr-trigger-{today - timedelta(days=31)}.jsonl"
    other = tmp_path / f"nr-trigger-{today - timedelta(days=32)}.jsonl"
    locked.touch()
    other.touch()
    unlink = Path.unlink

    def unlink_unless_locked(path, *args, **kwargs):
        if path == locked:
            raise PermissionError("file in use")
        return unlink(path, *args, **kwargs)

    monkeypatch.setattr(Path, "unlink", unlink_unless_locked)
    log = Logbook(tmp_path)
    try:
        assert locked.exists()
        assert not other.exists()
        assert locked.name in log.errors.get_nowait()

        log.emit("manual", operation="スケジューラ開始")
        log.flush()
        assert locked.name in log.errors.get_nowait()
        [current] = [path for path in tmp_path.glob("*.jsonl") if path != locked]
        record = json.loads(current.read_text(encoding="utf-8"))
        assert record["operation"] == "スケジューラ開始"
    finally:
        log.close()
