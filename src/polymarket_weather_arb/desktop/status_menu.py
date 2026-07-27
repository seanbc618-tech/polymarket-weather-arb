"""macOS status-item lifecycle shell.

Prefers AppKit (PyObjC). Falls back to a small Tk control window so packaged
builds always expose Open Dashboard / Pause / View Logs / Quit for beginners.
This is not a second cockpit UI.
"""

from __future__ import annotations

import logging
import threading
from typing import Callable

logger = logging.getLogger(__name__)


def run_status_menu(
    *,
    open_dashboard: Callable[[], None],
    pause_autopilot: Callable[[], None],
    view_logs: Callable[[], None],
    quit_app: Callable[[], None],
    stop_event: threading.Event,
) -> None:
    if _run_appkit_menu(
        open_dashboard=open_dashboard,
        pause_autopilot=pause_autopilot,
        view_logs=view_logs,
        quit_app=quit_app,
        stop_event=stop_event,
    ):
        return
    logger.warning("AppKit unavailable; using Tk lifecycle controls")
    _run_tk_controls(
        open_dashboard=open_dashboard,
        pause_autopilot=pause_autopilot,
        view_logs=view_logs,
        quit_app=quit_app,
        stop_event=stop_event,
    )


def _run_appkit_menu(
    *,
    open_dashboard: Callable[[], None],
    pause_autopilot: Callable[[], None],
    view_logs: Callable[[], None],
    quit_app: Callable[[], None],
    stop_event: threading.Event,
) -> bool:
    try:
        from AppKit import (  # type: ignore
            NSApplication,
            NSMenu,
            NSMenuItem,
            NSStatusBar,
            NSVariableStatusItemLength,
        )
        from Foundation import NSObject  # type: ignore
    except Exception:
        return False

    class StatusDelegate(NSObject):  # type: ignore[misc, valid-type]
        def openDashboard_(self, sender) -> None:  # noqa: N802
            open_dashboard()

        def pauseAutopilot_(self, sender) -> None:  # noqa: N802
            pause_autopilot()

        def viewLogs_(self, sender) -> None:  # noqa: N802
            view_logs()

        def quitApp_(self, sender) -> None:  # noqa: N802
            quit_app()
            app = NSApplication.sharedApplication()
            app.terminate_(None)

    app = NSApplication.sharedApplication()
    delegate = StatusDelegate.alloc().init()
    status_bar = NSStatusBar.systemStatusBar()
    item = status_bar.statusItemWithLength_(NSVariableStatusItemLength)
    item.setTitle_("PW")
    menu = NSMenu.alloc().init()

    def _add(title: str, action: str) -> None:
        menu_item = NSMenuItem.alloc().initWithTitle_action_keyEquivalent_(title, action, "")
        menu_item.setTarget_(delegate)
        menu.addItem_(menu_item)

    _add("Open Dashboard", "openDashboard:")
    _add("Pause Autopilot", "pauseAutopilot:")
    _add("View Logs", "viewLogs:")
    menu.addItem_(NSMenuItem.separatorItem())
    _add("Quit", "quitApp:")
    item.setMenu_(menu)

    def _watch() -> None:
        while not stop_event.is_set():
            stop_event.wait(0.5)
        try:
            app.terminate_(None)
        except Exception:
            pass

    threading.Thread(target=_watch, name="status-menu-watch", daemon=True).start()
    app.run()
    return True


def _run_tk_controls(
    *,
    open_dashboard: Callable[[], None],
    pause_autopilot: Callable[[], None],
    view_logs: Callable[[], None],
    quit_app: Callable[[], None],
    stop_event: threading.Event,
) -> None:
    try:
        import tkinter as tk
    except Exception:
        logger.exception("Tk unavailable; status controls disabled")
        while not stop_event.is_set():
            stop_event.wait(0.5)
        return

    root = tk.Tk()
    root.title("Polymarket Weather")
    root.geometry("280x220")
    root.resizable(False, False)

    tk.Label(root, text="Polymarket Weather", font=("Helvetica", 14, "bold")).pack(pady=12)
    tk.Label(root, text="Lifecycle controls (backend keeps running)").pack(pady=4)

    def _wrap(fn: Callable[[], None]) -> Callable[[], None]:
        def _inner() -> None:
            try:
                fn()
            except Exception:
                logger.exception("status control action failed")

        return _inner

    tk.Button(root, text="Open Dashboard", width=24, command=_wrap(open_dashboard)).pack(pady=4)
    tk.Button(root, text="Pause Autopilot", width=24, command=_wrap(pause_autopilot)).pack(pady=4)
    tk.Button(root, text="View Logs", width=24, command=_wrap(view_logs)).pack(pady=4)
    tk.Button(root, text="Quit", width=24, command=_wrap(lambda: (_safe_quit(quit_app, root)))).pack(
        pady=12
    )

    def _poll_stop() -> None:
        if stop_event.is_set():
            try:
                root.destroy()
            except Exception:
                pass
            return
        root.after(400, _poll_stop)

    root.protocol("WM_DELETE_WINDOW", lambda: _safe_quit(quit_app, root))
    root.after(400, _poll_stop)
    root.mainloop()


def _safe_quit(quit_app: Callable[[], None], root: object) -> None:
    try:
        quit_app()
    finally:
        try:
            root.destroy()  # type: ignore[attr-defined]
        except Exception:
            pass
