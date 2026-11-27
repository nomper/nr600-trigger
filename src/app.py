"""Tkinter entry point for the NR periodic trigger."""

import argparse
import ctypes
import os
import platform
import re
import sys
import time
import tkinter as tk
from dataclasses import replace
from pathlib import Path, PureWindowsPath
from queue import Empty, Queue
from threading import Thread
from tkinter import ttk

from . import single_instance
from .config import load_config, save_config
from .logbook import Logbook, local_now
from .logger_link import LoggerLink, Problem, STATE_NAMES
from .mock_link import MockLink
from .scheduler import Scheduler


BUILD = "step1"


def initial_anchor(saved_anchor: str | None, launch_text: str) -> str:
    return saved_anchor or launch_text


def _software_version(progid):
    if sys.platform != "win32":
        return None

    import winreg

    try:
        from win32api import GetFileVersionInfo
    except ImportError:
        return None

    def registry_value(key, view):
        with winreg.OpenKey(winreg.HKEY_CLASSES_ROOT, key, 0, winreg.KEY_READ | view) as handle:
            return winreg.QueryValueEx(handle, "")[0]

    for view in (0, winreg.KEY_WOW64_32KEY, winreg.KEY_WOW64_64KEY):
        try:
            clsid = registry_value(f"{progid}\\CLSID", view)
            command = registry_value(f"CLSID\\{clsid}\\LocalServer32", view)
            executable = re.match(r'^\s*"?(.+?\.exe)(?:"|\s|$)', command, re.IGNORECASE)
            if executable:
                path = os.path.expandvars(executable[1])
                version = GetFileVersionInfo(path, "\\")
                high, low = version["FileVersionMS"], version["FileVersionLS"]
                return ".".join(str(part) for part in (high >> 16, high & 65535, low >> 16, low & 65535))
        except OSError:
            continue
        except Exception as error:
            # pywintypes.error is a distinct exception for missing version resources.
            if not hasattr(error, "winerror"):
                raise
    return None


class App:
    def __init__(self, root, config_path, log_directory):
        self._launch_text = local_now().strftime("%Y%m%d %H%M%S")
        self.root = root
        self.config_path = config_path
        self.scheduler = self.log = self.config = None
        self._snapshot = self._stop = None
        self._close_results = Queue()
        self._closing = False
        self._cached_state = ""
        self._message = ""
        self.connection = tk.StringVar(value="未確立")
        self.details = tk.StringVar()
        self.state = tk.StringVar(value="—")
        self.mode = tk.StringVar(value="cron")
        self.cron = tk.StringVar()
        self.anchor = tk.StringVar()
        self.interval = tk.StringVar()
        self.lead = tk.StringVar()
        self.next_time = tk.StringVar(value="—")
        self.status = tk.StringVar(value="停止中")
        self._build()
        try:
            self.config = load_config(config_path)
            self.log = Logbook(log_directory, self.config.log_retention_days)
            link = MockLink(self.config, self.log) if self.config.mock else LoggerLink(self.config, self.log)
            self.scheduler = Scheduler(self.config, link, self.log)
            self.mode.set(self.config.mode)
            self.cron.set(self.config.cron)
            self.anchor.set(initial_anchor(self.config.anchor, self._launch_text))
            self.interval.set("" if self.config.interval_minutes is None else str(self.config.interval_minutes))
            self.lead.set(str(self.config.lead_seconds))
            settings = PureWindowsPath(self.config.settings_file).name
            self.details.set(f"{settings}  /  {self.config.logger_model} ID {self.config.unit_id}")
            interval_fields = {}
            if self.config.mode == "interval":
                interval_fields = {"anchor": self.config.anchor, "interval_minutes": self.config.interval_minutes}
            self.log.emit(
                "config", app_build=BUILD, windows_version=platform.win32_ver()[1] or None,
                software_version="mock" if self.config.mock else _software_version(self.config.progid),
                logger_model=self.config.logger_model, progid=self.config.progid,
                mode=self.config.mode, cron=self.config.cron,
                timezone=local_now().tzname(), timezone_names=time.tzname,
                lead_seconds=self.config.lead_seconds, **interval_fields,
            )
        except (OSError, ValueError) as error:
            self._message = f"設定を読み込めません。{error}"
        self._poll()
        self._poll_snapshot()

    def _build(self):
        self.root.title("NR-Trigger")
        self.root.minsize(640, 350)
        self.root.protocol("WM_DELETE_WINDOW", self._close)
        frame = ttk.Frame(self.root, padding=12)
        frame.pack(fill="both", expand=True)
        frame.columnconfigure(1, weight=1)
        frame.rowconfigure(7, weight=1)
        for row, (label, value) in enumerate((
            ("収集ソフト", self.connection), ("収集条件・機器", self.details),
            ("計測状態", self.state), ("次回発火", self.next_time), ("動作状態", self.status),
        )):
            ttk.Label(frame, text=label).grid(row=row, column=0, sticky="w", padx=(0, 10), pady=3)
            ttk.Label(frame, textvariable=value, wraplength=520).grid(row=row, column=1, sticky="w")
        editor = ttk.Frame(frame)
        editor.grid(row=5, column=0, columnspan=2, sticky="ew", pady=(8, 4))
        editor.columnconfigure(1, weight=1)
        ttk.Label(editor, text="方式").grid(row=0, column=0, sticky="w", padx=(0, 8))
        modes = ttk.Frame(editor)
        modes.grid(row=0, column=1, sticky="w")
        self.cron_radio = ttk.Radiobutton(modes, text="cron 指定", variable=self.mode, value="cron")
        self.interval_radio = ttk.Radiobutton(
            modes, text="間隔指定", variable=self.mode, value="interval", command=self._fill_anchor,
        )
        self.cron_radio.pack(side="left", padx=(0, 8))
        self.interval_radio.pack(side="left")
        ttk.Label(editor, text="リード秒数").grid(row=0, column=2, padx=8)
        self.lead_entry = ttk.Spinbox(editor, from_=3, to=5, textvariable=self.lead, width=4)
        self.lead_entry.grid(row=0, column=3)

        ttk.Label(editor, text="cron（秒先頭）").grid(row=1, column=0, sticky="w", padx=(0, 8))
        self.cron_entry = ttk.Entry(editor, textvariable=self.cron)
        self.cron_entry.grid(row=1, column=1, columnspan=3, sticky="ew", pady=3)

        ttk.Label(editor, text="開始日時").grid(row=2, column=0, sticky="w", padx=(0, 8))
        self.anchor_entry = ttk.Entry(editor, textvariable=self.anchor)
        self.anchor_entry.grid(row=2, column=1, sticky="ew")
        ttk.Label(editor, text="間隔（分）").grid(row=2, column=2, padx=8)
        self.interval_entry = ttk.Spinbox(editor, from_=1, to=99999, textvariable=self.interval, width=6)
        self.interval_entry.grid(row=2, column=3)

        controls = ttk.Frame(frame)
        controls.grid(row=6, columnspan=2, sticky="ew", pady=(3, 8))
        self.start_button = self._button(controls, "スケジューラ開始", self._start)
        self.stop_button = self._button(controls, "スケジューラ停止", self._stop_schedule)
        self.send_button = self._button(controls, "今すぐ送信", self._fire_now)
        self.collect_button = self._button(controls, "収集停止", self._stop_collect)
        self.close_button = self._button(controls, "終了", self._close)

        listing = ttk.Frame(frame)
        listing.grid(row=7, columnspan=2, sticky="nsew")
        self.table = ttk.Treeview(listing, columns=("scheduled", "sent", "result"), show="headings")
        for column, label, width in (
            ("scheduled", "予定時刻", 150), ("sent", "送信時刻", 150), ("result", "結果", 310),
        ):
            self.table.heading(column, text=label)
            self.table.column(column, width=width)
        scrollbar = ttk.Scrollbar(listing, orient="vertical", command=self.table.yview)
        self.table.configure(yscrollcommand=scrollbar.set)
        scrollbar.pack(side="right", fill="y")
        self.table.pack(fill="both", expand=True)

    @staticmethod
    def _button(parent, text, command):
        button = ttk.Button(parent, text=text, command=command)
        button.pack(side="left", padx=(0, 5))
        return button

    def _fill_anchor(self):
        if not self.anchor.get().strip():
            self.anchor.set(self._launch_text)

    def _start(self):
        try:
            mode = self.mode.get()
            interval = self.interval.get().strip()
            try:
                interval_minutes = int(interval) if interval else None
            except ValueError:
                if mode == "interval":
                    raise
                # An inactive interval value must not prevent starting cron.
                interval_minutes = self.config.interval_minutes
            anchor = self.config.anchor
            if mode == "interval":
                anchor = self.anchor.get().strip() or None
            config = replace(
                self.config, mode=mode, cron=self.cron.get(), anchor=anchor,
                interval_minutes=interval_minutes, lead_seconds=int(self.lead.get()),
            )
            if config != self.config:
                save_config(config, self.config_path)
                self.config = config
        except (OSError, ValueError) as error:
            self._message = str(error)
            return
        self._message = ""
        self.scheduler.start(config)

    def _stop_schedule(self):
        self._message = ""
        self.scheduler.stop()

    def _fire_now(self):
        self._message = ""
        self.scheduler.send_now()

    def _stop_collect(self):
        self._message = ""
        if self._stop is None:
            self._stop = self.scheduler.stop_collect()
            if self._stop is None:
                self._message = "収集停止を依頼できません。未接続または処理中です"

    def _close(self):
        if self._closing:
            return
        if self.scheduler is None:
            self.root.destroy()
            return
        self._closing = True
        self._message = ""

        def close():
            self._close_results.put(self.scheduler.close())

        Thread(target=close, name="app-close", daemon=True).start()

    def _poll_snapshot(self):
        if self.scheduler and not self._closing and self._snapshot is None:
            self._snapshot = self.scheduler.snapshot()
        self.root.after(1000, self._poll_snapshot)

    def _poll(self):
        if self.log:
            self._show_logs()
        wait_reason = None
        if self.scheduler:
            if self._snapshot is not None and self._snapshot.done():
                value = self._snapshot.result()
                self._snapshot = None
                if isinstance(value, dict) and value.get("state") is not None:
                    number = value["state"]
                    label = STATE_NAMES.get(number, value.get("error_kind", "未知の状態"))
                    self._cached_state = f"{label}（{number}）"
                    if value.get("module_name"):
                        self._cached_state += f"  /  {value['module_name']}"
            if self._stop is not None and self._stop.done():
                value = self._stop.result()
                self._stop = None
                if isinstance(value, Problem):
                    self._message = f"収集停止失敗。{value.detail}"
            wait_reason = self.scheduler.link_wait_reason
            if wait_reason:
                self.connection.set(wait_reason)
                self._cached_state = ""
            elif self.scheduler.link.connected:
                self.connection.set("確立済み")
            else:
                self.connection.set("未確立")
                self._cached_state = ""
            self.state.set(self._cached_state or "—")
            next_at = self.scheduler.next_at
            self.next_time.set(next_at.isoformat(sep=" ", timespec="seconds") if next_at else "—")

        try:
            closed = self._close_results.get_nowait()
        except Empty:
            pass
        else:
            if closed:
                self.log.close()
                self.root.destroy()
                return
            self._closing = False
            self._message = "終了保留。応答または収集・読み出しの完了を待っています"

        ready = self.scheduler is not None and not self._closing
        running = ready and self.scheduler.active
        editable = ready and not running
        mode = self.mode.get()
        status = self.scheduler.status if self.scheduler else "停止中"
        if self._closing:
            status = "終了処理中"
        elif self._message and status != wait_reason:
            status = self._message
        self.status.set(status)
        for widget, enabled in (
            (self.start_button, editable), (self.stop_button, running),
            (self.send_button, ready),
            (self.collect_button, ready and self._stop is None and self.scheduler.link.connected),
            (self.close_button, not self._closing),
            (self.cron_radio, editable), (self.interval_radio, editable), (self.lead_entry, editable),
            (self.cron_entry, editable and mode == "cron"),
            (self.anchor_entry, editable and mode == "interval"),
            (self.interval_entry, editable and mode == "interval"),
        ):
            widget.state(("!disabled",) if enabled else ("disabled",))
        self.root.after(100, self._poll)

    def _show_logs(self):
        for _ in range(100):
            try:
                event = self.log.results.get_nowait()
            except Empty:
                break
            scheduled = event["scheduled_at"].strftime("%m-%d %H:%M:%S.%f")[:-3]
            sent = event.get("call_started_at")
            sent_text = sent.strftime("%m-%d %H:%M:%S.%f")[:-3] if sent else "—"
            self.table.insert(
                "", 0, values=(scheduled, sent_text, f"{event['result']}  {event['reason']}"),
            )
            rows = self.table.get_children()
            if len(rows) > 200:
                self.table.delete(rows[-1])
        try:
            error = self.log.errors.get_nowait()
        except Empty:
            pass
        else:
            self._message = f"ログ処理でエラーが発生しました。{error}"


def main(argv=None):
    directory = Path(sys.executable).resolve().parent if getattr(sys, "frozen", False) else Path(__file__).resolve().parents[1]
    parser = argparse.ArgumentParser(description="NR-Trigger")
    parser.add_argument("--config", type=Path, default=directory / "config.json")
    args = parser.parse_args(argv)
    instance_handle = single_instance.acquire()  # Keep it alive through mainloop.
    if instance_handle is None:
        try:
            ctypes.windll.user32.MessageBoxW(
                None, "NR Trigger はすでに起動しています。", "NR Trigger", 0x40,  # MB_OK | MB_ICONINFORMATION
            )
        except Exception:
            # Notification failure must still stop the duplicate instance.
            pass
        return

    root = tk.Tk()
    App(root, args.config, args.config.resolve().parent / "logs")
    root.mainloop()


if __name__ == "__main__":
    main()
