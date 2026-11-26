"""Run the same link operations against a configurable in-process server."""

from threading import Event
from time import sleep

from .config import MockConfig
from .logger_link import LoggerLink, Timing, _Worker


class _Disconnected(Exception):
    hresult = -2147417848  # RPC_E_DISCONNECTED


class MockLink(LoggerLink):
    def __init__(self, config, log, *, scenario=None, timing=Timing()):
        super().__init__(config, log, timing=timing)
        self.scenario = scenario or config.mock or MockConfig()
        self.start_called = Event()
        self.resume_start = Event()
        self.start_count = 0
        self.quit_count = 0
        self.state = 1
        self.disconnect = Event()
        self._disconnect_once = self.scenario.scenario == "disconnect"

    def _new_worker(self):
        return _Worker(self._release)

    def _dispatch(self):
        return _MockServer(self)


class _MockServer:
    def __init__(self, link):
        self.link = link
        self.identifier = None
        self.DisplayAlerts = True
        self.Visible = False
        self.established = False
        self.started = False
        self.set_busy = link.scenario.scenario == "busy"

    def Initialize(self):
        pass

    def SetIdentifier(self, value):
        self.identifier = value
        return 0

    def GetIdentifier(self):
        return self.identifier

    def OpenFile(self, path):
        return 0

    def GetState(self):
        link = self.link
        scenario = link.scenario.scenario
        if self.established:
            if link.disconnect.is_set() or link._disconnect_once:
                link.disconnect.clear()
                link._disconnect_once = False
                raise _Disconnected("mock: サーバー切断")
            if scenario == "delay":
                sleep(link.scenario.delay_seconds)
            if scenario == "disconnect_after_start" and self.started:
                raise _Disconnected("mock: 観測中に切断")
            if self.set_busy:
                self.set_busy = False
                link.state = 4
        self.established = True
        return link.state

    def Start(self):
        link = self.link
        link.start_count += 1
        link.start_called.set()
        scenario = link.scenario.scenario
        if scenario == "delay":
            sleep(link.scenario.delay_seconds)
        if scenario == "no_response":
            link.resume_start.wait()
        if scenario == "disconnect_on_start":
            raise _Disconnected("mock: Start 中に切断")
        if scenario == "start_failure":
            return -3
        if scenario not in {"unconfirmed", "disconnect_after_start"}:
            link.state = 11 if scenario == "error_stop" else 4
        self.started = True
        return 0

    def GetErrorCode(self):
        return 42

    def GetModuleName(self):
        return "Mock WAVE LOGGER"

    def GetUnitInfo(self, unit):
        return 194 if self.link.config.logger_model == "NR-600" else 193

    def Stop(self):
        self.link.state = 1

    def Quit(self):
        self.link.quit_count += 1
