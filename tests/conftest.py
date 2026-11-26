from pathlib import Path
import sys

import pytest

# Also allow running the pytest console script from a clean checkout.
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.config import Config


@pytest.fixture
def config():
    return Config(
        version=1, progid="WaveLoggerPro2Demo.Application",
        settings_file=r"C:\test\condition.xxx", logger_model="NR-600",
        unit_id=0, cron="0 * * * * *",
    )
