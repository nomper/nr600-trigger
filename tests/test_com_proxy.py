"""The real server exposes no-argument members as COM properties.

pywin32 returns their values on attribute access, so _ComObject must
present them as zero-argument callables while passing real methods and
attribute writes through unchanged.
"""

import pytest

from src.logger_link import _ComObject


class FakeServer:
    def __init__(self):
        self.starts = 0
        self.identifier = 255
        self.Visible = True

    @property
    def GetState(self):
        return 1

    @property
    def GetActiveFileType(self):
        return 2

    @property
    def GetIdentifier(self):
        return self.identifier

    @property
    def Start(self):
        self.starts += 1
        return 0

    def SetIdentifier(self, value):
        self.identifier = value
        return 0


def test_property_members_become_callables():
    proxy = _ComObject(FakeServer())
    assert proxy.GetState() == 1
    assert proxy.GetActiveFileType() == 2
    assert proxy.GetIdentifier() == 255


def test_start_property_executes_once_per_call():
    server = FakeServer()
    proxy = _ComObject(server)
    assert proxy.Start() == 0
    assert server.starts == 1
    assert proxy.Start() == 0
    assert server.starts == 2


def test_methods_and_writes_pass_through():
    server = FakeServer()
    proxy = _ComObject(server)
    assert proxy.SetIdentifier(4) == 0
    assert proxy.GetIdentifier() == 4
    proxy.Visible = False
    assert server.Visible is False


def test_property_rejects_arguments():
    proxy = _ComObject(FakeServer())
    with pytest.raises(TypeError):
        proxy.GetState(1)
