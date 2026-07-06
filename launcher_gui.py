"""ClaudeBridge desktop launcher — a small window that starts everything and
shows a QR code + link so you can open the app on your phone.

Runs the FastAPI bridge and a pure-Python localtunnel client in background
threads (no Node, no console). Packaged to a single .exe with PyInstaller.
"""
from __future__ import annotations

import asyncio
import os
import socket
import subprocess
import sys
import threading
import urllib.request
from pathlib import Path

# Make `bridge` importable both from source and from a PyInstaller bundle.
_ROOT = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parent))
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

# In a --windowed build there is no console, so sys.stdout/stderr are None.
# Libraries that write to them (uvicorn logging) would crash the bridge thread.
# Redirect to a log file next to the exe so everything keeps working.
if sys.stdout is None or sys.stderr is None:
    try:
        _base = Path(sys.executable).resolve().parent if getattr(sys, "frozen", False) else _ROOT
        _logf = open(_base / "claudebridge.log", "a", buffering=1, encoding="utf-8", errors="replace")
    except Exception:
        import io
        _logf = io.StringIO()
    if sys.stdout is None:
        sys.stdout = _logf
    if sys.stderr is None:
        sys.stderr = _logf

import tkinter as tk
from tkinter import font as tkfont

PORT = 8765
DBG_PORT = 9222
BG = "#0b0d10"
PANEL = "#14181d"
FG = "#e6e8eb"
MUTED = "#8a8f96"
ACCENT = "#d97757"
OK = "#2ea043"
ERR = "#ff7b72"


def _port_open(port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.settimeout(0.5)
        return s.connect_ex(("127.0.0.1", port)) == 0


def _find_vscode() -> str | None:
    """Locate VS Code (or Insiders) anywhere it's normally installed:
    registry (App Paths + Uninstall) → common folders → PATH."""
    import shutil

    cands: list[str] = []

    # macOS: the app bundle + the `code` CLI.
    if sys.platform == "darwin":
        for c in (
            "/Applications/Visual Studio Code.app/Contents/MacOS/Electron",
            os.path.expanduser("~/Applications/Visual Studio Code.app/Contents/MacOS/Electron"),
            "/Applications/Visual Studio Code - Insiders.app/Contents/MacOS/Electron",
            shutil.which("code"), shutil.which("code-insiders"),
        ):
            if c and os.path.isfile(c):
                return c
        return None

    # 1. Registry — App Paths (exact exe) and Uninstall (InstallLocation).
    try:
        import winreg
        for hive in (winreg.HKEY_CURRENT_USER, winreg.HKEY_LOCAL_MACHINE):
            for exe in ("Code.exe", "Code - Insiders.exe"):
                try:
                    with winreg.OpenKey(hive, r"SOFTWARE\Microsoft\Windows\CurrentVersion\App Paths\\" + exe) as k:
                        val, _ = winreg.QueryValueEx(k, None)
                        if val:
                            cands.append(val)
                except OSError:
                    pass
            try:
                with winreg.OpenKey(hive, r"SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall") as u:
                    i = 0
                    while True:
                        try:
                            name = winreg.EnumKey(u, i); i += 1
                        except OSError:
                            break
                        try:
                            with winreg.OpenKey(u, name) as k:
                                dn, _ = winreg.QueryValueEx(k, "DisplayName")
                                if "Visual Studio Code" in dn:
                                    loc, _ = winreg.QueryValueEx(k, "InstallLocation")
                                    exe = "Code - Insiders.exe" if "Insiders" in dn else "Code.exe"
                                    cands.append(str(Path(loc) / exe))
                        except OSError:
                            pass
            except OSError:
                pass
    except Exception:
        pass

    # 2. Common fixed locations (user + system + Insiders).
    la = os.environ.get("LOCALAPPDATA", "")
    pf = os.environ.get("ProgramFiles", "")
    pf86 = os.environ.get("ProgramFiles(x86)", "")
    cands += [
        os.path.join(la, r"Programs\Microsoft VS Code\Code.exe"),
        os.path.join(pf, r"Microsoft VS Code\Code.exe"),
        os.path.join(pf86, r"Microsoft VS Code\Code.exe"),
        os.path.join(la, r"Programs\Microsoft VS Code Insiders\Code - Insiders.exe"),
        os.path.join(pf, r"Microsoft VS Code Insiders\Code - Insiders.exe"),
    ]

    # 3. PATH: the `code` / `code.cmd` shim lives in <root>\bin, exe one level up.
    for name in ("code.cmd", "code", "code-insiders.cmd"):
        p = shutil.which(name)
        if p:
            root = Path(p).resolve().parent.parent
            cands.append(str(root / "Code.exe"))
            cands.append(str(root / "Code - Insiders.exe"))

    for c in cands:
        if c and os.path.isfile(c):
            return c
    return None


_RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
_APP_NAME = "ClaudeBridge"


def _autostart_target() -> str:
    # The exe when frozen; in source mode, launch this script with pythonw.
    if getattr(sys, "frozen", False):
        return f'"{sys.executable}"'
    return f'"{sys.executable}" "{Path(__file__).resolve()}"'


def _mac_plist() -> Path:
    return Path.home() / "Library" / "LaunchAgents" / "com.claudebridge.launcher.plist"


def is_autostart() -> bool:
    if sys.platform.startswith("win"):
        try:
            import winreg
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, _RUN_KEY) as k:
                val, _ = winreg.QueryValueEx(k, _APP_NAME)
                return bool(val)
        except OSError:
            return False
    if sys.platform == "darwin":
        return _mac_plist().exists()
    return False


def set_autostart(on: bool) -> None:
    if sys.platform.startswith("win"):
        import winreg
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, _RUN_KEY, 0, winreg.KEY_SET_VALUE) as k:
            if on:
                winreg.SetValueEx(k, _APP_NAME, 0, winreg.REG_SZ, _autostart_target())
            else:
                try:
                    winreg.DeleteValue(k, _APP_NAME)
                except OSError:
                    pass
        return
    if sys.platform == "darwin":
        plist = _mac_plist()
        if on:
            plist.parent.mkdir(parents=True, exist_ok=True)
            exe = sys.executable if getattr(sys, "frozen", False) else sys.executable
            plist.write_text(
                '<?xml version="1.0" encoding="UTF-8"?>\n'
                '<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" '
                '"http://www.apple.com/DTDs/PropertyList-1.0.dtd">\n'
                '<plist version="1.0"><dict>\n'
                '  <key>Label</key><string>com.claudebridge.launcher</string>\n'
                f'  <key>ProgramArguments</key><array><string>{exe}</string></array>\n'
                '  <key>RunAtLoad</key><true/>\n'
                '</dict></plist>\n', encoding="utf-8")
        else:
            try:
                plist.unlink()
            except OSError:
                pass


def _public_ip() -> str:
    try:
        with urllib.request.urlopen("https://api.ipify.org", timeout=8) as r:
            return r.read().decode().strip()
    except Exception:
        return ""


class App:
    def __init__(self) -> None:
        from bridge import auth  # imported here so sys.path is set up first
        self.token = auth.TOKEN
        self.url = ""
        self.full_url = ""
        self.ip = ""

        self.root = tk.Tk()
        self.root.title("ClaudeBridge")
        self.root.configure(bg=BG)
        self.root.geometry("380x600")
        self.root.resizable(False, False)

        big = tkfont.Font(family="Segoe UI", size=15, weight="bold")
        small = tkfont.Font(family="Segoe UI", size=9)
        mono = tkfont.Font(family="Consolas", size=10)

        from bridge.version import VERSION
        tk.Label(self.root, text=f"ClaudeBridge v{VERSION}", bg=BG, fg=FG, font=big).pack(pady=(16, 2))
        self.status = tk.Label(self.root, text="● запускаю…", bg=BG, fg=MUTED, font=small)
        self.status.pack()

        # Update banner — hidden until a newer release is found on GitHub.
        self.update_btn = tk.Button(self.root, text="", command=self.do_update,
                                    bg=OK, fg="white", relief="flat", font=small,
                                    activebackground=OK, cursor="hand2")
        self._pending_update = None

        self.canvas = tk.Canvas(self.root, width=280, height=280, bg="white", highlightthickness=0)
        self.canvas.pack(pady=14)
        self.canvas.create_text(140, 140, text="…", fill="#999", font=big)

        tk.Label(self.root, text="Ссылка (если нет камеры — скопируй):",
                 bg=BG, fg=MUTED, font=small).pack()
        self.url_entry = tk.Entry(self.root, font=mono, width=42, bg=PANEL, fg=FG,
                                  readonlybackground=PANEL, relief="flat", justify="center")
        self.url_entry.pack(pady=(4, 6), padx=14, fill="x")
        self.url_entry.insert(0, "…")
        self.url_entry.config(state="readonly")

        self.copy_btn = tk.Button(self.root, text="Копировать ссылку", command=self.copy,
                                  bg=ACCENT, fg="white", relief="flat", font=small,
                                  activebackground=ACCENT, cursor="hand2")
        self.copy_btn.pack(pady=(0, 8), ipady=4, padx=14, fill="x")

        self.hint = tk.Label(self.root, text="", bg=BG, fg=MUTED, font=small, wraplength=340, justify="center")
        self.hint.pack(pady=(2, 6), padx=12)

        self.vscode = tk.Label(self.root, text="", bg=BG, fg=MUTED, font=small)
        self.vscode.pack(side="bottom", pady=(2, 8))

        # Autostart with Windows — so a self-reboot brings ClaudeBridge back.
        self.autostart_var = tk.BooleanVar(value=is_autostart())
        self.autostart_cb = tk.Checkbutton(
            self.root, text="Запускать при включении ПК", variable=self.autostart_var,
            command=self.toggle_autostart, bg=BG, fg=FG, selectcolor=PANEL,
            activebackground=BG, activeforeground=FG, font=small,
            highlightthickness=0, bd=0, cursor="hand2")
        self.autostart_cb.pack(side="bottom", pady=(0, 2))

        self.root.protocol("WM_DELETE_WINDOW", self.on_close)
        self.root.after(200, self.boot)

    def toggle_autostart(self) -> None:
        try:
            set_autostart(self.autostart_var.get())
        except Exception:
            self.autostart_var.set(is_autostart())

    def on_close(self) -> None:
        # Best-effort: tell the phone the PC is going down (clean shutdown/close).
        try:
            from bridge import push
            push._send_blocking({"title": "ClaudeBridge",
                                 "body": "ПК выключается — связь пропадёт",
                                 "tag": "cb-off", "url": "/"})
        except Exception:
            pass
        self.root.destroy()

    # ── startup ─────────────────────────────────────────────────
    def boot(self) -> None:
        self.check_vscode()
        threading.Thread(target=self.start_bridge, daemon=True).start()
        threading.Thread(target=self.start_tunnel, daemon=True).start()
        threading.Thread(target=self._fetch_ip, daemon=True).start()
        threading.Thread(target=self.check_updates, daemon=True).start()

    # ── auto-update from GitHub Releases ─────────────────────────
    def check_updates(self) -> None:
        try:
            from bridge import updater
            info = updater.check_latest()
            if info:
                self.root.after(0, lambda: self._show_update(info))
        except Exception:
            pass
        # re-check once a day while the app stays running
        self.root.after(24 * 60 * 60 * 1000,
                        lambda: threading.Thread(target=self.check_updates, daemon=True).start())

    def _show_update(self, info: dict) -> None:
        self._pending_update = info
        self.update_btn.config(text=f"🔄 Обновить до v{info['version']}", state="normal")
        self.update_btn.pack(pady=(2, 4), ipady=3, padx=14, fill="x")

    def do_update(self) -> None:
        info = self._pending_update
        if not info:
            return
        if not info.get("asset"):  # no binary for this OS — open the release page
            import webbrowser
            webbrowser.open(info.get("url", ""))
            return
        self.update_btn.config(text="Скачиваю обновление…", state="disabled")
        threading.Thread(target=self._do_update_thread, args=(info,), daemon=True).start()

    def _do_update_thread(self, info: dict) -> None:
        from bridge import updater
        ok = updater.apply_update(info["asset"])
        if ok:
            self.root.after(300, self.on_close)   # helper swaps + relaunches the new build
        else:
            self.root.after(0, lambda: self.update_btn.config(text="Ошибка обновления", state="normal"))

    def check_vscode(self) -> None:
        if _port_open(DBG_PORT):
            self.vscode.config(text="VS Code: отладка активна ✓", fg=OK, cursor="")
            self.vscode.unbind("<Button-1>")
        else:
            self.vscode.config(text="VS Code без отладки — нажми, чтобы перезапустить",
                               fg=ERR, cursor="hand2")
            self.vscode.bind("<Button-1>", lambda e: self.relaunch_vscode())
        # Re-check periodically: VS Code may be relaunched without the flag later,
        # which silently kills tab visibility on the phone.
        self.root.after(15000, self.check_vscode)

    def relaunch_vscode(self) -> None:
        code = _find_vscode()
        if not code:
            self.vscode.config(text="VS Code не найден — установи его", fg=ERR)
            return
        if sys.platform == "darwin":
            # Quit VS Code, then relaunch it with the debug port.
            subprocess.Popen(["osascript", "-e", 'quit app "Visual Studio Code"'],
                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            self.root.after(2500, lambda: subprocess.Popen(
                [code, f"--remote-debugging-port={DBG_PORT}"]))
        else:
            img = os.path.basename(code)  # "Code.exe" or "Code - Insiders.exe"
            subprocess.Popen(["taskkill", "/IM", img, "/F"],
                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, shell=False)
            self.root.after(2500, lambda: subprocess.Popen(
                [code, f"--remote-debugging-port={DBG_PORT}"], shell=False))
        self.root.after(6000, self.check_vscode)

    def start_bridge(self) -> None:
        try:
            import uvicorn
            from bridge.main import app
            config = uvicorn.Config(app, host="0.0.0.0", port=PORT, log_level="warning")
            server = uvicorn.Server(config)
            server.install_signal_handlers = lambda: None
            server.run()
        except Exception as e:  # surface fatal bridge errors in the UI
            self.root.after(0, lambda: self.status.config(text=f"● ошибка моста: {e}", fg=ERR))

    def start_tunnel(self) -> None:
        from bridge import lt_client
        # wait until the bridge is accepting connections
        for _ in range(40):
            if _port_open(PORT):
                break
            threading.Event().wait(0.25)

        def on_url(u: str) -> None:
            self.root.after(0, lambda: self.set_url(u))

        sub = lt_client.stable_subdomain(self.token)  # stable URL across restarts
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        while True:
            try:
                loop.run_until_complete(lt_client.run_tunnel(PORT, on_url=on_url, subdomain=sub))
            except Exception:
                pass
            self.root.after(0, lambda: self.status.config(text="● переподключаю туннель…", fg=MUTED))
            threading.Event().wait(2.0)

    def _fetch_ip(self) -> None:
        ip = _public_ip()
        if ip:
            self.ip = ip
            self.root.after(0, self._update_hint)

    # ── ui updates ──────────────────────────────────────────────
    def set_url(self, url: str) -> None:
        self.url = url
        self.full_url = f"{url}/?key={self.token}"
        try:  # expose the current URL for debugging / external tools
            base = Path(sys.executable).resolve().parent if getattr(sys, "frozen", False) else _ROOT
            (base / ".bridge-url").write_text(self.full_url, encoding="utf-8")
        except Exception:
            pass
        self.status.config(text="● онлайн", fg=OK)
        self.url_entry.config(state="normal")
        self.url_entry.delete(0, "end")
        self.url_entry.insert(0, self.full_url)
        self.url_entry.config(state="readonly")
        self.draw_qr(self.full_url)
        self._update_hint()

    def _update_hint(self) -> None:
        ip = self.ip or "твой публичный IP"
        self.hint.config(text=("При первом заходе localtunnel попросит ввести адрес — введи:\n"
                               f"{ip}\nОдин раз, дальше запомнит."))

    def draw_qr(self, data: str, size: int = 280) -> None:
        import qrcode
        q = qrcode.QRCode(border=2, box_size=1, error_correction=qrcode.constants.ERROR_CORRECT_M)
        q.add_data(data)
        q.make(fit=True)
        m = q.get_matrix()
        n = len(m)
        cell = max(1, size // n)
        span = cell * n
        off = (size - span) // 2
        c = self.canvas
        c.delete("all")
        c.create_rectangle(0, 0, size, size, fill="white", outline="white")
        for y, row in enumerate(m):
            for x, val in enumerate(row):
                if val:
                    c.create_rectangle(off + x * cell, off + y * cell,
                                       off + (x + 1) * cell, off + (y + 1) * cell,
                                       fill="black", outline="black")

    def copy(self) -> None:
        if not self.full_url:
            return
        self.root.clipboard_clear()
        self.root.clipboard_append(self.full_url)
        self.copy_btn.config(text="Скопировано ✓")
        self.root.after(1500, lambda: self.copy_btn.config(text="Копировать ссылку"))

    def run(self) -> None:
        self.root.mainloop()


if __name__ == "__main__":
    App().run()
