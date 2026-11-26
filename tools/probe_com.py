"""Probe a Windows WAVE LOGGER automation server by ProgID."""

import argparse
import sys
from time import perf_counter


def _measure(name, operation):
    started = perf_counter()
    try:
        result = operation()
    except Exception as error:
        elapsed = (perf_counter() - started) * 1000
        hresult = getattr(error, "hresult", None)
        suffix = f" (HRESULT=0x{hresult & 0xFFFFFFFF:08X})" if hresult is not None else ""
        print(f"{name}: {elapsed:.3f} ms | ERROR {error}{suffix}", flush=True)
        return False, None
    elapsed = (perf_counter() - started) * 1000
    print(f"{name}: {elapsed:.3f} ms | {result!r}", flush=True)
    return True, result


def _read(server, name):
    member = getattr(server, name)
    return member() if callable(member) else member


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("progid", help="e.g. WaveLoggerPro2Demo.Application")
    args = parser.parse_args(argv)
    if sys.platform != "win32":
        parser.exit(1, "This probe requires Windows and pywin32.\n")
    try:
        import pythoncom
        from win32com.client import DispatchEx
    except ImportError as error:
        parser.exit(1, f"Could not load pywin32: {error}\n")

    pythoncom.CoInitializeEx(pythoncom.COINIT_APARTMENTTHREADED)
    server = None
    success = False
    try:
        success, server = _measure("DispatchEx", lambda: DispatchEx(args.progid))
        if success:
            success, _ = _measure("Initialize", lambda: server.Initialize())
            if success:
                # GetModuleName / GetState / GetErrorCode are COM properties
                # on this server: attribute access already returns the value.
                for name in ("GetModuleName", "GetState", "GetErrorCode"):
                    ok, _ = _measure(name, lambda: _read(server, name))
                    success = success and ok
    finally:
        if server is not None:
            ok, _ = _measure("Quit", lambda: server.Quit())
            success = success and ok
            server = None
        pythoncom.CoUninitialize()
    return 0 if success else 1


if __name__ == "__main__":
    sys.exit(main())
