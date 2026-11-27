"""Load and save the version 1 settings file."""

import json
import math
import re
from calendar import monthrange
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta
from functools import lru_cache
from pathlib import Path
from tempfile import NamedTemporaryFile

from croniter import CroniterError, croniter


MOCK_SCENARIOS = frozenset({
    "normal", "delay", "unconfirmed", "no_response", "disconnect",
    "disconnect_on_start", "disconnect_after_start", "busy",
    "start_failure", "error_stop",
})
LOGGER_MODELS = ("NR-500", "NR-600", "NR-X100")


_SOFTWARE_BY_PROGID = {
    "WaveLoggerPro2.Application": "PRO",
    "WaveLoggerPro2Demo.Application": "PRO",
    "WaveLoggerX.Application": "X",
    "WaveLoggerXDemo.Application": "X",
    "WaveLogger2.Application": "無印",
    "WaveLogger2Demo.Application": "無印",
}
_MODELS_BY_SOFTWARE = {
    "PRO": ("NR-500", "NR-600"),
    "X": ("NR-500", "NR-X100"),
    "無印": ("NR-500",),
}
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


def parse_anchor(text: str) -> datetime:
    """Parse an anchor as a local wall-clock time."""
    if isinstance(text, str):
        text = text.strip()
        for pattern, date_format in (
            (r"[0-9]{8} [0-9]{6}", "%Y%m%d %H%M%S"),
            (r"[0-9]{4}-[0-9]{2}-[0-9]{2} [0-9]{2}:[0-9]{2}:[0-9]{2}", "%Y-%m-%d %H:%M:%S"),
            (r"[0-9]{4}/[0-9]{2}/[0-9]{2} [0-9]{2}:[0-9]{2}:[0-9]{2}", "%Y/%m/%d %H:%M:%S"),
        ):
            if re.fullmatch(pattern, text):
                try:
                    return datetime.strptime(text, date_format)
                except ValueError:
                    break
    raise ValueError(
        "anchor（開始日時）は有効な日時を yyyymmdd hhmmss、yyyy-mm-dd hh:mm:ss、"
        "または yyyy/mm/dd hh:mm:ss で指定してください"
    )


def interval_next(anchor: datetime, interval_minutes: int, base: datetime) -> datetime:
    """Return the first slot strictly after base on the anchor's interval grid."""
    if base < anchor:
        return anchor
    interval = timedelta(minutes=interval_minutes)
    return anchor + interval * ((base - anchor) // interval + 1)


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
    cron: str = ""
    mode: str = "cron"
    anchor: str | None = None
    interval_minutes: int | None = None
    lead_seconds: int = 5
    visible: bool = False
    log_retention_days: int = 30
    mock: MockConfig | None = None
    link_mode: str = "full"

    def __post_init__(self):
        if type(self.version) is not int or self.version != 1:
            raise ValueError("version は 1 を指定してください")
        for field in ("progid", "settings_file"):
            value = getattr(self, field)
            if not isinstance(value, str) or not value.strip():
                raise ValueError(f"{field} は空でない文字列を指定してください")
        if self.logger_model not in LOGGER_MODELS:
            raise ValueError(f"logger_model は {'、'.join(LOGGER_MODELS)} のいずれかを指定してください")
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
        if self.mode not in ("cron", "interval"):
            raise ValueError("mode は cron または interval を指定してください")
        if self.link_mode not in ("full", "simple"):
            raise ValueError("link_mode は full または simple を指定してください")
        software = self.software
        if software is not None:
            models = _MODELS_BY_SOFTWARE[software]
            if self.logger_model not in models:
                raise ValueError(
                    f"WAVE LOGGER {software} では logger_model に {self.logger_model} を指定できません。"
                    f"対応機種は {'、'.join(models)} です"
                )
            if software == "無印" and self.link_mode == "full":
                raise ValueError(
                    "無印 WAVE LOGGER には GetActiveFileType がないため link_mode に full を指定できません。"
                    "simple を指定してください"
                )
        if self.mode == "cron":
            if not isinstance(self.cron, str) or not self.cron.strip():
                raise ValueError("cron は空でない文字列を指定してください")
            try:
                cron_schedule(self.cron, datetime.now()).get_next(datetime)
            except (CroniterError, ValueError, OverflowError) as error:
                raise ValueError(f"cron が不正です: {error}") from error
        else:
            anchor = parse_anchor(self.anchor)
            if type(self.interval_minutes) is not int or self.interval_minutes < 1:
                raise ValueError("interval_minutes（間隔）は 1 以上の整数を指定してください")
            try:
                interval_next(anchor, self.interval_minutes, max(anchor, datetime.now()))
            except OverflowError as error:
                raise ValueError("interval_minutes（間隔）が大きすぎます") from error

    def next_fire_after(self, base: datetime) -> datetime:
        if self.mode == "cron":
            return cron_schedule(self.cron, base).get_next(datetime)
        return interval_next(parse_anchor(self.anchor), self.interval_minutes, base)

    @property
    def software(self) -> str | None:
        return _SOFTWARE_BY_PROGID.get(self.progid)

    @property
    def identifier(self) -> int:
        return self.unit_id + (4 if self.logger_model in ("NR-600", "NR-X100") else 0)

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

        NR-X100 assumes 8 + unit_id by analogy with the NR-600 trial
        result.  This is unverified in WAVE LOGGER X and requires
        acceptance testing on actual hardware and the X trial server.
        """
        return self.unit_id + (8 if self.logger_model in ("NR-600", "NR-X100") else 0)


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
