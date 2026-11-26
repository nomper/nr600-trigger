"""Keep one NR Trigger instance per Windows session."""

MUTEX_NAME = r"Local\NRTrigger.SingleInstance"
ERROR_ALREADY_EXISTS = 183
_UNAVAILABLE = object()


def acquire(name=MUTEX_NAME):
    """Return a handle to keep alive, or None if an instance already exists."""
    try:
        from win32api import GetLastError
        from win32event import CreateMutex
    except ImportError:
        return _UNAVAILABLE

    # The open handle keeps the named object alive without taking ownership.
    handle = CreateMutex(None, False, name)
    if GetLastError() == ERROR_ALREADY_EXISTS:
        return None  # PyHANDLE closes the duplicate handle when discarded.
    return handle
