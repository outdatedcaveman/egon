"""Egon native desktop app — entry point.

Run from source:
    .venv\\Scripts\\python.exe -m egon_app.main

When bundled by PyInstaller (build_exe.py), this is the script argument.

Single-instance guard via QLocalServer/QLocalSocket so double-clicking the
shortcut focuses the existing window instead of spawning a second app.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

# ---------------------------------------------------------------------------
# GLOBAL: silence every subprocess this app or its descendants ever spawn.
# Bruno's hard rule: no console windows pop up, ever. We monkey-patch
# subprocess.Popen.__init__ before any other code runs so adb.exe, schtasks,
# git, anything — all of it stays hidden. Applies to direct calls AND library
# code we don't control (zeroconf, requests' bundled CA helpers, etc.).
# ---------------------------------------------------------------------------
if sys.platform == "win32":
    import subprocess as _sp
    _CREATE_NO_WINDOW = 0x08000000
    _orig_popen_init = _sp.Popen.__init__

    def _silent_popen_init(self, *args, **kwargs):
        flags = kwargs.get("creationflags", 0) | _CREATE_NO_WINDOW
        kwargs["creationflags"] = flags
        # Also suppress startup-info window if a caller passes one
        si = kwargs.get("startupinfo")
        if si is None:
            si = _sp.STARTUPINFO()
        si.dwFlags |= _sp.STARTF_USESHOWWINDOW
        si.wShowWindow = 0  # SW_HIDE
        kwargs["startupinfo"] = si
        return _orig_popen_init(self, *args, **kwargs)

    _sp.Popen.__init__ = _silent_popen_init

from PySide6.QtCore import Qt, QSharedMemory, QCoreApplication, QEvent, QObject
from PySide6.QtGui import QIcon
from PySide6.QtNetwork import QLocalServer, QLocalSocket
from PySide6.QtWidgets import QApplication, QMessageBox

# Ensure egon root on path even when run as script (not module)
_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from egon_app.window import MainWindow
from egon_app.health import start_health_server

APP_ID = "com.outdatedcaveman.egon"
ICON_PATH = _ROOT / "shell" / "egon.ico"


def _set_appusermodelid() -> None:
    """Tell Windows this is its own app, not a child of python.exe — so it
    gets its own taskbar group, its own icon, can be pinned independently."""
    try:
        import ctypes
        ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID(APP_ID)
    except Exception:
        pass


def _trace(msg: str) -> None:
    """Temporary boot tracer — writes milestones to logs/boot-trace.log so we
    can see how far a pythonw launch gets (stderr is swallowed under pythonw).
    Guarded by EGON_BOOT_TRACE=1. Bruno 2026-05-29 crash debug."""
    if os.environ.get("EGON_BOOT_TRACE") != "1":
        return
    try:
        from datetime import datetime
        p = _ROOT / "logs" / "boot-trace.log"
        with p.open("a", encoding="utf-8") as f:
            f.write(f"{datetime.now().isoformat(timespec='milliseconds')}  {msg}\n")
    except Exception:
        pass


def _mind_ready(timeout_s: float = 2.0) -> bool:
    try:
        import urllib.request
        with urllib.request.urlopen(
            "http://127.0.0.1:8000/api/v1/mind/stats",
            timeout=timeout_s,
        ) as r:
            return 200 <= int(getattr(r, "status", 0)) < 300
    except Exception:
        return False


def _mind_service_python() -> str:
    pyw = _ROOT / ".venv" / "Scripts" / "pythonw.exe"
    if pyw.exists():
        return str(pyw)
    py = _ROOT / ".venv" / "Scripts" / "python.exe"
    if py.exists():
        return str(py)
    return sys.executable


def _start_mind_service(log_fn=None) -> bool:
    if _mind_ready():
        return True
    script = _ROOT / "scripts" / "mind_service.py"
    if not script.exists():
        if log_fn:
            log_fn("error", event="mind_service_missing", path=str(script))
        return False
    try:
        import subprocess
        env = os.environ.copy()
        env["PYTHONDONTWRITEBYTECODE"] = "1"
        env["PYTHONPATH"] = str(_ROOT) + os.pathsep + env.get("PYTHONPATH", "")
        env["EGON_MIND_SERVICE_FORCE"] = "1"
        kwargs = {
            "cwd": str(_ROOT),
            "stdin": subprocess.DEVNULL,
            "stdout": subprocess.DEVNULL,
            "stderr": subprocess.DEVNULL,
            "env": env,
        }
        if sys.platform == "win32":
            kwargs["creationflags"] = (
                getattr(subprocess, "CREATE_NO_WINDOW", 0)
                | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
                | 0x00000008
            )
        subprocess.Popen([_mind_service_python(), str(script)], **kwargs)
        if log_fn:
            log_fn("info", event="mind_service_start_requested", script=str(script))
        return True
    except Exception as e:
        if log_fn:
            log_fn("error", event="mind_service_start_failed",
                   error=f"{type(e).__name__}: {str(e)[:240]}")
        return False


class EgonScrollTamer(QObject):
    """Event filter to intercept wheel events and tame scroll speed inside Egon.
    Uses a low multiplier (0.1) so touchpad scrolling feels precise and
    controlled, matching the speed Bruno tuned for Antigravity/Claude.
    """
    def eventFilter(self, obj: QObject, event: QEvent) -> bool:
        if event.type() == QEvent.Type.Wheel:
            from PySide6.QtWidgets import QAbstractScrollArea
            parent = obj.parent()
            if isinstance(parent, QAbstractScrollArea) or isinstance(obj, QAbstractScrollArea):
                scroll_area = parent if isinstance(parent, QAbstractScrollArea) else obj
                
                # Check vertical scroll
                v_delta = event.angleDelta().y()
                if v_delta != 0:
                    scrollbar = scroll_area.verticalScrollBar()
                    if scrollbar and scrollbar.isVisible():
                        # Tame scroll step (multiplier=0.1 — Bruno 2026-06-23)
                        multiplier = 0.1
                        step = max(scrollbar.singleStep() * multiplier, 3)
                        current = scrollbar.value()
                        new_val = current - (v_delta / 120) * step
                        scrollbar.setValue(int(max(scrollbar.minimum(), min(scrollbar.maximum(), new_val))))
                        return True
                        
                # Check horizontal scroll
                h_delta = event.angleDelta().x()
                if h_delta != 0:
                    scrollbar = scroll_area.horizontalScrollBar()
                    if scrollbar and scrollbar.isVisible():
                        multiplier = 0.1
                        step = max(scrollbar.singleStep() * multiplier, 3)
                        current = scrollbar.value()
                        new_val = current - (h_delta / 120) * step
                        scrollbar.setValue(int(max(scrollbar.minimum(), min(scrollbar.maximum(), new_val))))
                        return True
        return super().eventFilter(obj, event)


def main() -> int:
    _trace("main() entered")
    _set_appusermodelid()

    os.environ["QT_QUICK_BACKEND"] = "software"
    os.environ["QSG_RHI_PREFER_SOFTWARE_RENDERER"] = "1"
    QCoreApplication.setAttribute(Qt.AA_UseSoftwareOpenGL)
    app = QApplication(sys.argv)
    _tamer = EgonScrollTamer()
    app.installEventFilter(_tamer)
    app._egon_scroll_tamer = _tamer  # type: ignore[attr-defined]
    _trace("QApplication created")
    app.setApplicationName("Egon")
    app.setOrganizationName("outdatedcaveman")
    app.setApplicationDisplayName("Egon")

    if ICON_PATH.exists():
        app.setWindowIcon(QIcon(str(ICON_PATH)))

    # ---- single-instance guard ----
    # Bruno 2026-05-29: bulletproof layer 0 — Windows kernel named mutex via
    # CreateMutexW. The QLocalServer dance below works for the common case
    # but has a small race window. The kernel mutex closes that completely:
    # the OS itself serializes ownership, so exactly one process can hold the
    # name "Local\Egon-2026-05" at a time. If we lose the claim, we still try
    # the QLocalSocket "focus" path so the existing instance comes forward.
    SINGLE_INSTANCE_NAME = "egon-app-single-instance-2026-05"
    _shared_key = "egon-app-single-instance-key"
    _use_recovery = False

    try:
        from lib.single_instance_mutex import claim_or_exit
        _claimed = claim_or_exit("Egon-2026-05")
        _trace(f"mutex claim_or_exit -> {_claimed}")
        if not _claimed:
            # Someone else owns the mutex. Try to focus them.
            _sk = QLocalSocket()
            _sk.connectToServer(SINGLE_INSTANCE_NAME)
            _focused = False
            if _sk.waitForConnected(400):
                _sk.write(b"focus\n"); _sk.flush()
                _sk.waitForBytesWritten(400)
                if _sk.waitForReadyRead(400):
                    _resp = bytes(_sk.readAll())
                    if b"acknowledged" in _resp:
                        _focused = True
                _sk.disconnectFromServer()
            
            if _focused:
                return 0
            else:
                # Connection failed or did not respond. Treat the mutex owner
                # as authoritative and exit instead of spawning a recovery UI:
                # duplicate Egon instances can run unattended Panop services
                # and are more dangerous than a failed focus request.
                try:
                    import time
                    for _ in range(60):
                        if _mind_ready(timeout_s=0.5):
                            return 0
                        time.sleep(0.5)
                except Exception:
                    pass
                if os.environ.get("EGON_ALLOW_RECOVERY_INSTANCE") != "1":
                    return 0
                _claimed_rec = claim_or_exit("Egon-2026-05-recovery")
                _trace(f"recovery mutex claim_or_exit -> {_claimed_rec}")
                if _claimed_rec:
                    _use_recovery = True
                    SINGLE_INSTANCE_NAME = "egon-app-single-instance-2026-05-recovery"
                    _shared_key = "egon-app-single-instance-key-recovery"
                else:
                    # Even the recovery instance is claimed. Try to focus it.
                    _sk2 = QLocalSocket()
                    _sk2.connectToServer("egon-app-single-instance-2026-05-recovery")
                    if _sk2.waitForConnected(400):
                        _sk2.write(b"focus\n"); _sk2.flush()
                        _sk2.waitForBytesWritten(400)
                        _sk2.disconnectFromServer()
                    return 0
    except Exception:
        # Mutex layer best-effort; fall through to QLocalServer.
        pass

    # Bruno 2026-05-27: previously used QSharedMemory("egon-app-single-instance-key").
    # That guard had two real problems: (1) a crashed Egon could leave the
    # shared memory segment in a state that let a second instance create
    # one too (two Egons were observed running at the same time, which was
    # the upstream cause of duplicate Panop spawns), and (2) a second
    # launch just popped a dialog and exited — it didn't *focus* the
    # already-running window, which is the actual thing the user wants.
    #
    # New approach: QLocalServer/QLocalSocket. The first instance listens
    # on a named pipe / Unix socket; any later launch connects and sends
    # "focus", which the first instance handles by raising its window.
    # Crash recovery is straightforward: a dead server isn't listening
    # any more, so the connect attempt fails and the new instance
    # correctly takes over. The QSharedMemory guard is kept alongside
    # as defence-in-depth (cheap, harmless if it works, ignored if not).
    sock = QLocalSocket()
    sock.connectToServer(SINGLE_INSTANCE_NAME)
    if sock.waitForConnected(400):
        # Existing instance reachable — tell it to focus, then exit cleanly.
        sock.write(b"focus\n")
        sock.flush()
        sock.waitForBytesWritten(400)
        _focused = False
        if sock.waitForReadyRead(400):
            _resp = bytes(sock.readAll())
            if b"acknowledged" in _resp:
                _focused = True
        sock.disconnectFromServer()
        if _focused:
            return 0

    # No existing instance reachable. Bruno 2026-05-29: previously we
    # called QLocalServer.removeServer() preemptively here, which WIPES the
    # other Egon's freshly-bound socket if two processes raced. That broke
    # the guard. New approach: try listen() first; only call removeServer
    # if listen explicitly fails because of a stale socket.
    _single_server = QLocalServer()
    if not _single_server.listen(SINGLE_INSTANCE_NAME):
        # Could be a stale socket from a crashed prior run, or another
        # racing Egon already bound. Try one more time after a remove.
        QLocalServer.removeServer(SINGLE_INSTANCE_NAME)
    if not _single_server.isListening() and not _single_server.listen(SINGLE_INSTANCE_NAME):
        # Edge: someone else just won the race. Treat as already-running.
        sock2 = QLocalSocket()
        sock2.connectToServer(SINGLE_INSTANCE_NAME)
        if sock2.waitForConnected(400):
            sock2.write(b"focus\n")
            sock2.flush()
            sock2.waitForBytesWritten(400)
            _focused = False
            if sock2.waitForReadyRead(400):
                _resp = bytes(sock2.readAll())
                if b"acknowledged" in _resp:
                    _focused = True
            sock2.disconnectFromServer()
            if _focused:
                return 0
        # Couldn't connect either. Fail closed unless explicitly debugging:
        # proceeding without the guard can create duplicate native apps.
        if os.environ.get("EGON_ALLOW_RECOVERY_INSTANCE") != "1":
            return 0

    # Defence-in-depth: keep the old QSharedMemory key too. If both fail
    # we still let the launch through; better an extra instance than no Egon.
    _shared = QSharedMemory(_shared_key)
    _shared.create(1)

    app._egon_single_server = _single_server  # type: ignore[attr-defined]
    app._egon_shared = _shared                # type: ignore[attr-defined]

    def _on_focus_request():
        sk = _single_server.nextPendingConnection()
        if sk is None:
            return
        def _read_and_focus():
            data = bytes(sk.readAll()).strip()
            win = getattr(app, "_egon_main_window", None)
            if data.startswith(b"focus") and win is not None:
                win.show()
                win.setWindowState(
                    (win.windowState() & ~Qt.WindowMinimized) | Qt.WindowActive)
                win.raise_()
                win.activateWindow()
            try:
                sk.write(b"acknowledged\n")
                sk.flush()
                sk.waitForBytesWritten(200)
                sk.disconnectFromServer()
            except Exception: pass
        sk.readyRead.connect(_read_and_focus)
        # Also handle the case where the socket already has data buffered.
        if sk.bytesAvailable() > 0:
            _read_and_focus()

    _single_server.newConnection.connect(_on_focus_request)
    _trace("single-instance guard passed; starting services")

    health = start_health_server()
    _trace("health server started")

    # ---- Standalone mind supervisor -----------------------------------------
    # The shared mind must not depend on the Inbox/Panop UI being open. Launch
    # the guarded standalone service first; if :8000 is already owned by the
    # in-process Panop app or another healthy mind service, this is a no-op.
    def _mind_log(level, **kw):
        try:
            from datetime import datetime
            line = f"{datetime.now().isoformat(timespec='seconds')} [{level}] " + \
                   " ".join(f"{k}={v}" for k, v in kw.items())
            with (_ROOT / "logs" / "mind-service-bootstrap.log").open("a", encoding="utf-8") as f:
                f.write(line + "\n")
        except Exception:
            pass

    def _ensure_mind_service():
        _start_mind_service(log_fn=_mind_log)

    import threading as _th
    _th.Thread(target=_ensure_mind_service, daemon=True, name="mind-service-bootstrap").start()

    # ---- Panop supervisor ----------------------------------------------------
    # The native app never started Panop, so the Kindle/Instapaper/Paperpile
    # harvest endpoints (served by Panop on :8000) went dead whenever Panop
    # wasn't already running — making those tabs blank. Bootstrap it now and
    # re-check every 60s, respawning if it died. Bruno 2026-05-22.
    def _panop_log(level, **kw):
        # Structured one-liner to logs/panop-inproc.log so we can diagnose
        # why the in-process server does/doesn't bind :8000 (stderr is gone
        # under pythonw). Bruno 2026-05-29. Cheap, always on.
        try:
            from datetime import datetime
            line = f"{datetime.now().isoformat(timespec='seconds')} [{level}] " + \
                   " ".join(f"{k}={v}" for k, v in kw.items())
            with (_ROOT / "logs" / "panop-inproc.log").open("a", encoding="utf-8") as f:
                f.write(line + "\n")
        except Exception:
            pass

    def _ensure_panop():
        try:
            from lib import panop_proc
            panop_proc.ensure_running(log_fn=_panop_log)
        except Exception as e:
            _panop_log("error", event="ensure_panop_exception",
                       error=f"{type(e).__name__}: {str(e)[:240]}")
    _th.Thread(target=_ensure_panop, daemon=True, name="panop-bootstrap").start()

    # ---- Phone keepalive (in-process service) --------------------------------
    # Phase 2 of the 2026-05-27 embedding: replaces the standalone
    # scripts/phone_keepalive.py daemon. Runs ONLY while Egon is open and
    # dies with the process — matches the "nothing runs outside Egon" rule.
    # The standalone script is intentionally left in place (additive
    # principle), just no longer auto-started.
    try:
        from egon_app.services.phone_keepalive_service import PhoneKeepaliveService
        _phone_keepalive = PhoneKeepaliveService()
        _phone_keepalive.start()
        app.aboutToQuit.connect(_phone_keepalive.stop)
        app._egon_phone_keepalive = _phone_keepalive  # type: ignore[attr-defined]
    except Exception:
        pass

    # ---- Daily 06:00 pass service (in-app QTimer; only while Egon is open) ---
    # Bruno's 2026-05-27 rule: the daily pass routine is kept, but never via a
    # Windows scheduled task — only a QTimer that ticks while Egon's UI is
    # open. If Egon is closed at 06:00, nothing fires that day.
    # Catch-up: if Egon launches between 06:00 and 12:00, fires once on launch.
    try:
        from egon_app.services.daily_pass_service import DailyPassService
        _daily_pass = DailyPassService(parent=app)
        _daily_pass.start()
        app.aboutToQuit.connect(_daily_pass.stop)
        app._egon_daily_pass = _daily_pass  # type: ignore[attr-defined]
    except Exception:
        pass

    # ---- Phone alert (tray notification when the phone link needs you) ------
    # Bruno 2026-06-01: Egon must ALWAYS tell him when the phone needs a manual
    # USB re-plug (tcpip mode is lost on reboot / dev-options toggle). The
    # keepalive writes state/panop/phone_status.json; this pops a native tray
    # toast on the transition into "needs action" so he's told even when not on
    # the Inbox tab.
    try:
        from egon_app.services.phone_alert_service import PhoneAlertService
        _phone_alert = PhoneAlertService(app)
        _phone_alert.start()
        app.aboutToQuit.connect(_phone_alert.stop)
        app._egon_phone_alert = _phone_alert  # type: ignore[attr-defined]
    except Exception:
        pass

    # ---- Panop drain service (the phone-tab routine, in-app scheduler) -------
    # Bruno 2026-06-01 (VITAL): runs the ORIGINAL Panop capture routine —
    # discover phone → wake Chrome → drain → fetch → classify into the
    # predefined categories → restore point → save to bookmarks + Zotero →
    # gated clean — on Panop's own interval (default 6 h) while Egon is open,
    # with a catch-up on launch if due. This replaces the disabled scheduled
    # task with an in-Egon timer (nothing runs outside Egon). The routine and
    # ALL its safety live UNCHANGED in lib/adapters/panop_capture.run_capture()
    # → run_adb_sweep(); this service only triggers it. Dies with Egon.
    try:
        from egon_app.services.panop_drain_service import PanopDrainService
        _panop_drain = PanopDrainService(parent=app)
        _panop_drain.start()
        app.aboutToQuit.connect(_panop_drain.stop)
        app._egon_panop_drain = _panop_drain  # type: ignore[attr-defined]
    except Exception:
        pass

    # ---- Mind ingestion service ---------------------------------------------
    # Phase A of the unified-mind plan (2026-05-28): poll Claude Code, Codex
    # and Antigravity memory dirs every 60 s while Egon is open. New
    # transcripts/notes become rows in state/mind.db via the local
    # /api/v1/mind/* endpoints. Dies with Egon — matches the "nothing runs
    # outside Egon" rule. Same lifecycle shape as the phone keepalive.
    try:
        from lib.mind_ingest import MindIngestService
        _mind_ingest = MindIngestService()
        _mind_ingest.start()
        app.aboutToQuit.connect(_mind_ingest.stop)
        app._egon_mind_ingest = _mind_ingest  # type: ignore[attr-defined]
    except Exception:
        pass

    # ---- Routster supervisor (Egon-managed Electron subprocess) --------------
    # Phase 3 of the 2026-05-27 embedding (Routster, not Mouseion — Bruno's
    # correction). Routster is Electron/Node so it CAN'T live inside Egon's
    # process the way Panop now does. The next best thing: a supervised
    # subprocess Egon spawns at startup and terminates on aboutToQuit, so
    # it still dies with Egon. Idempotent — if Routster is already serving
    # on :4000 (Bruno launched it directly, or it was left over), we don't
    # double-spawn. See lib/routster_proc.py for the lifecycle.
    # Opt-out: egon-config.json {"routster": {"autostart": false}} keeps
    # Routster fully manual (Bruno asked why it always rises with Egon,
    # 2026-06-11 — answer: Phase 3 embedding; this flag is the off switch).
    try:
        from lib import routster_proc
        _r_cfg = {}
        try:
            import json as _json
            _r_cfg = (_json.loads((_ROOT / "egon-config.json").read_text(
                encoding="utf-8")).get("routster") or {})
        except Exception:
            pass
        if _r_cfg.get("autostart", True):
            routster_proc.ensure_running_async(log_fn=lambda l, **k: None)
            app.aboutToQuit.connect(routster_proc.stop)
    except Exception:
        pass

    # ---- Headroom supervisor (Egon-managed context compression proxy) --------
    # Spawns local headroom proxy server on :8787 for LLM context compression
    # across all local agent runs. Dies with Egon. Idempotent.
    try:
        from lib import headroom_proc
        headroom_proc.ensure_running_async(log_fn=lambda l, **k: None)
        app.aboutToQuit.connect(headroom_proc.stop)
    except Exception:
        pass

    _trace("about to construct MainWindow()")
    win = MainWindow()
    _trace("MainWindow() constructed")
    # Expose the MainWindow so the single-instance focus handler can raise
    # it when a second launch sends "focus". Bruno 2026-05-27.
    app._egon_main_window = win  # type: ignore[attr-defined]
    if ICON_PATH.exists():
        win.setWindowIcon(QIcon(str(ICON_PATH)))
    if health:
        win.setProperty("egon_health_url", f"http://{health[0]}:{health[1]}/health")
    win.show()
    _trace(f"win.show() called — visible={win.isVisible()} size={win.size().width()}x{win.size().height()}")

    from PySide6.QtCore import QTimer as _QTimer
    _panop_timer = _QTimer()
    _panop_timer.setInterval(60_000)   # re-check Panop every 60s
    _panop_timer.timeout.connect(
        lambda: _th.Thread(target=_ensure_panop, daemon=True).start())
    _panop_timer.start()
    app._egon_panop_timer = _panop_timer  # type: ignore[attr-defined]

    _mind_timer = _QTimer()
    _mind_timer.setInterval(60_000)
    _mind_timer.timeout.connect(
        lambda: _th.Thread(target=_ensure_mind_service, daemon=True).start())
    _mind_timer.start()
    app._egon_mind_service_timer = _mind_timer  # type: ignore[attr-defined]

    def _ensure_headroom():
        try:
            from lib import headroom_proc
            headroom_proc.ensure_running()
        except Exception:
            pass

    _headroom_timer = _QTimer()
    _headroom_timer.setInterval(60_000)   # re-check Headroom every 60s
    _headroom_timer.timeout.connect(
        lambda: _th.Thread(target=_ensure_headroom, daemon=True).start())
    _headroom_timer.start()
    app._egon_headroom_timer = _headroom_timer  # type: ignore[attr-defined]

    return app.exec()


if __name__ == "__main__":
    raise SystemExit(main())
