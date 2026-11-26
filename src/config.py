"""Load and save the version 1 settings file."""

import json
import math
import re
from calendar import monthrange
from dataclasses import asdict, dataclass
from datetime import datetime
from functools import lru_cache
from pathlib import Path
from tempfile import NamedTemporaryFile

from croniter import CroniterError, croniter


MOCK_SCENARIOS = frozenset({
    "normal", "delay", "unconfirmed", "no_response", "disconnect",
    "disconnect_on_start", "disconnect_after_start", "busy",
    "start_failure", "error_stop",
})


_CRON_RANGES = ((0, 59), (0, 59), (0, 23), (1, 31), (1, 12), (0, 7))
_CRON_ITEM = re.compile(r"(\*|[0-9]+(?:-[0-9]+)?)(?:/([0-9]+))?")


def _cron_values(field, low, high):
    values = set()
    for item in field.split(","):
        match = _CRON_ITEM.fullmatch(item)
        if match is None:
            raise ValueError(f"フィールド {field!r} の記法が不正です")
        span, stride = match.groups()
        step = int(stride or "1")
        if span == "*":
            first, last = low, high
        else:
            left, _, right = span.partition("-")
            first, last = int(left), int(right or left)
        if not (step > 0 and low <= first <= last <= high):
            raise ValueError(f"フィールド {field!r} の範囲または刻みが不正です")
        values.update(range(first, last + 1, step))
    return values


@lru_cache(maxsize=128)
def _normalize_cron(expression):
    fields = expression.split()
    if len(fields) != 6:
        raise ValueError("cron は秒を先頭とする 6 フィールドを指定してください")

    # Normalize TB Sync's numeric syntax before handing off calendar iteration.
    values = [_cron_values(field, low, high) for field, (low, high) in zip(fields, _CRON_RANGES)]
    if 7 in values[5]:
        values[5].remove(7)
        values[5].add(0)
    day_or = not (fields[3].startswith("*") or fields[5].startswith("*"))
    normalized = [
        "*" if field == "*" else ",".join(map(str, sorted(numbers)))
        for field, numbers in zip(fields, values)
    ]
    if day_or:
        if len(values[3]) == 31 or len(values[5]) == 7:
            normalized[3] = normalized[5] = "*"
        elif min(values[3]) > max(monthrange(2000, month)[1] for month in values[4]):
            # In an OR, an impossible day of month leaves the weekday branch.
            normalized[3] = "*"
    return " ".join(normalized), day_or


def cron_schedule(expression: str, base: datetime):
    normalized, day_or = _normalize_cron(expression)
    return croniter(normalized, base, second_at_beginning=True, day_or=day_or)


@dataclass(frozen=True)
class MockConfig:
    scenario: str = "normal"
    delay_seconds: float = 0.5

    def __post_init__(self):
        if not isinstance(self.scenario, str) or self.scenario not in MOCK_SCENARIOS:
            raise ValueError("mock.scenario が不正です")
        delay = self.delay_seconds
        if type(delay) not in (int, float) or not 0 <= delay < math.inf:
            raise ValueError("mock.delay_seconds は有限の非負数を指定してください")


@dataclass(frozen=True)
class Config:
    version: int
    progid: str
    settings_file: str
    logger_model: str
    unit_id: int
    cron: str
    lead_seconds: int = 5
    visible: bool = True
    log_retention_days: int = 30
    mock: MockConfig | None = None

    def __post_init__(self):
        if type(self.version) is not int or self.version != 1:
            raise ValueError("version は 1 を指定してください")
        for field in ("progid", "settings_file", "cron"):
            value = getattr(self, field)
            if not isinstance(value, str) or not value.strip():
                raise ValueError(f"{field} は空でない文字列を指定してください")
        if self.logger_model not in ("NR-500", "NR-600"):
            raise ValueError("logger_model は NR-500 または NR-600 を指定してください")
        if type(self.unit_id) is not int or not 0 <= self.unit_id <= 3:
            raise ValueError("unit_id は 0〜3 の整数を指定してください")
        if type(self.lead_seconds) is not int or not 3 <= self.lead_seconds <= 5:
            raise ValueError("lead_seconds は 3〜5 の整数を指定してください")
        if type(self.visible) is not bool:
            raise ValueError("visible は true または false を指定してください")
        if type(self.log_retention_days) is not int or self.log_retention_days < 1:
            raise ValueError("log_retention_days は 1 以上の整数を指定してください")
        if self.mock is not None and not isinstance(self.mock, MockConfig):
            raise ValueError("mock は設定オブジェクトを指定してください")
        try:
            cron_schedule(self.cron, datetime.now()).get_next(datetime)
        except (CroniterError, ValueError, OverflowError) as error:
            raise ValueError(f"cron が不正です: {error}") from error

    @property
    def identifier(self) -> int:
        return self.unit_id + (4 if self.logger_model == "NR-600" else 0)

    @property
    def expected_identifier(self) -> int:
        """Value GetIdentifier must return for establish to succeed.

        This is a different numbering space from the SetIdentifier
        argument.  Observed on the trial server (tanaka3, 2026-11-26):
        NR-600 reported 8 + unit_id for both USB and LAN settings, so
        the establish check compares against this expected value rather
        than the value passed to SetIdentifier.  The trial cannot
        emulate an NR-500, so NR-500 follows the manual (the physical
        ID itself) until the product unit is checked at acceptance.
        """
        return self.unit_id + (8 if self.logger_model == "NR-600" else 0)


def load_config(path: Path | str) -> Config:
    with Path(path).open(encoding="utf-8-sig") as file:
        data = json.load(file)
    if not isinstance(data, dict):
        raise ValueError("設定のルートは JSON オブジェクトを指定してください")
    try:
        if "mock" in data:
            if not isinstance(data["mock"], dict):
                raise ValueError("mock は JSON オブジェクトを指定してください")
            data["mock"] = MockConfig(**data["mock"])
        return Config(**data)
    except TypeError as error:
        raise ValueError(f"設定項目が不正です: {error}") from error


def save_config(config: Config, path: Path | str) -> None:
    path = Path(path)
    data = asdict(config)
    if config.mock is None:
        del data["mock"]
    temporary = None
    try:
        with NamedTemporaryFile(
            mode="w", dir=path.parent, encoding="utf-8", delete=False
        ) as file:
            temporary = Path(file.name)
            json.dump(data, file, ensure_ascii=False, indent=2)
            file.write("\n")
        temporary.replace(path)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)
