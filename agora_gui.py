import ctypes
import json
import os
from pathlib import Path
import queue
import re
import subprocess
import sys
import threading
import traceback
import urllib.request
import webview

from agora_bot import (
    APP_VERSION,
    DEFAULT_BASE_MODEL,
    DEFAULT_EFFORT,
    DEFAULT_MODEL,
    DEFAULT_SUCCESS_RATE,
    GITHUB_REPO,
    fetch_grouped_models,
    get_app_dir,
    read_config_raw,
    run_bot,
    save_config,
)


def get_ui_path() -> str:
    """Retourne le chemin vers ui/index.html (PyInstaller ou source)."""
    base = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parent))
    return str(base / "ui" / "index.html")


def parse_version(tag: str) -> tuple[int, ...]:
    """Convertit 'v1.1.0' en tuple (1, 1, 0) pour comparaison."""
    nums = [int(x) for x in re.findall(r"\d+", tag or "")]
    return tuple(nums) if nums else (0,)


def show_fatal_error(app_dir: Path, err_text: str) -> None:
    """Écrit l'erreur dans agora_bot_error.log et affiche une boîte de dialogue Windows."""
    try:
        (app_dir / "agora_bot_error.log").write_text(err_text, encoding="utf-8")
    except Exception:
        pass
    if sys.platform == "win32":
        try:
            ctypes.windll.user32.MessageBoxW(
                0,
                f"Impossible de démarrer AgoraQuiz Bot :\n\n{err_text[:1000]}",
                "AgoraQuiz Bot — Erreur",
                0x10,
            )
        except Exception:
            pass


class BotApi:
    """API exposée à l'interface WebView2."""

    def __init__(self) -> None:
        self.app_dir = get_app_dir()
        self._log_queue: queue.Queue[str] = queue.Queue()
        self._stop_event = threading.Event()
        self._thread: threading.Thread | None = None
        self._running = False
        self._error = ""
        self._summary = ""

    def _log(self, msg: str) -> None:
        text = str(msg)
        if ">>>" in text:
            self._summary = text.replace(">>>", "").replace("<<<", "").strip()
        self._log_queue.put(text)

    def get_initial_state(self) -> dict:
        models, agy_status = fetch_grouped_models()
        return {
            "version": APP_VERSION,
            "is_frozen": getattr(sys, "frozen", False),
            "config": read_config_raw(self.app_dir),
            "models": models,
            "agy_status": agy_status,
            "data_dir": str(self.app_dir),
            "default_base_model": DEFAULT_BASE_MODEL,
            "default_effort": DEFAULT_EFFORT,
            "default_success_rate": DEFAULT_SUCCESS_RATE,
        }

    def check_update(self) -> dict:
        """Vérifie si une release plus récente existe sur GitHub."""
        url = f"https://api.github.com/repos/{GITHUB_REPO}/releases/latest"
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "AgoraQuizBot"})
            with urllib.request.urlopen(req, timeout=8) as resp:
                data = json.loads(resp.read().decode("utf-8"))
            tag = data.get("tag_name", "")
            assets = data.get("assets", [])
            exe_url = ""
            for asset in assets:
                if asset.get("name", "").lower().endswith(".exe"):
                    exe_url = asset.get("browser_download_url", "")
                    break
            available = parse_version(tag) > parse_version(APP_VERSION) and bool(exe_url)
            return {
                "ok": True,
                "available": available,
                "current": APP_VERSION,
                "latest": tag or f"v{APP_VERSION}",
                "download_url": exe_url,
                "release_url": data.get("html_url", ""),
            }
        except Exception as e:
            return {"ok": False, "available": False, "current": APP_VERSION, "error": str(e)}

    def apply_update(self, download_url: str) -> dict:
        """Télécharge le nouvel .exe et lance son remplacement automatique."""
        if not getattr(sys, "frozen", False):
            return {
                "ok": False,
                "error": "Mise à jour automatique disponible uniquement depuis AgoraQuizBot.exe (utilisez git pull en mode script).",
            }
        if not download_url:
            return {"ok": False, "error": "URL de téléchargement manquante."}

        exe_path = Path(sys.executable).resolve()
        new_path = exe_path.with_name(exe_path.stem + ".new.exe")
        bat_path = exe_path.with_name("_update_agora.bat")

        try:
            req = urllib.request.Request(download_url, headers={"User-Agent": "AgoraQuizBot"})
            with urllib.request.urlopen(req, timeout=120) as resp:
                payload = resp.read()
            if len(payload) < 1_000_000 or not payload.startswith(b"MZ"):
                return {"ok": False, "error": "Fichier téléchargé invalide."}
            new_path.write_bytes(payload)

            bat_content = (
                "@echo off\r\n"
                "timeout /t 2 /nobreak >nul\r\n"
                f'move /y "{new_path}" "{exe_path}" >nul\r\n'
                f'start "" "{exe_path}"\r\n'
                'del "%~f0"\r\n'
            )
            bat_path.write_text(bat_content, encoding="utf-8")

            flags = subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0
            subprocess.Popen(["cmd.exe", "/c", str(bat_path)], creationflags=flags)

            def close_soon() -> None:
                try:
                    for w in webview.windows:
                        w.destroy()
                except Exception:
                    pass
                os._exit(0)

            threading.Timer(0.4, close_soon).start()
            return {"ok": True}
        except Exception as e:
            return {"ok": False, "error": str(e)}

    def start(self, opts: dict) -> dict:
        if self._running:
            return {"ok": False, "error": "Le bot est déjà en cours d'exécution."}

        try:
            cfg = {
                "username": opts.get("username", "").strip(),
                "password": opts.get("password", ""),
                "quiz_name": opts.get("quiz_name", "").strip(),
            }
            save_config(self.app_dir, cfg)
        except Exception as e:
            return {"ok": False, "error": f"Impossible d'enregistrer la config : {e}"}

        while not self._log_queue.empty():
            self._log_queue.get_nowait()

        self._stop_event.clear()
        self._running = True
        self._error = ""
        self._summary = ""

        def worker() -> None:
            try:
                run_bot(
                    username=cfg["username"],
                    password=cfg["password"],
                    mode=opts.get("mode", "adventure"),
                    model=opts.get("model", DEFAULT_MODEL),
                    effort=opts.get("effort", DEFAULT_EFFORT),
                    headless=not bool(opts.get("show_browser", True)),
                    count=max(0, int(opts.get("count", 1))),
                    quiz_name=cfg["quiz_name"],
                    solo_type=opts.get("solo_type", "Premier à 10"),
                    success_rate=int(opts.get("success_rate", DEFAULT_SUCCESS_RATE)),
                    ephemeral=bool(opts.get("ephemeral", True)),
                    app_dir=self.app_dir,
                    log=self._log,
                    stop_event=self._stop_event,
                )
            except Exception as e:
                self._error = str(e)
                self._log(f"Erreur : {e}")
            finally:
                self._running = False

        self._thread = threading.Thread(target=worker, daemon=True)
        self._thread.start()
        return {"ok": True}

    def stop(self) -> dict:
        self._stop_event.set()
        self._log("Signal d'arrêt envoyé...")
        return {"ok": True}

    def poll(self) -> dict:
        lines = []
        while not self._log_queue.empty():
            lines.append(self._log_queue.get_nowait())
        return {
            "running": self._running,
            "error": self._error,
            "summary": self._summary,
            "lines": lines,
        }


def main() -> None:
    app_dir = get_app_dir()
    try:
        api = BotApi()
        webview.create_window(
            "AgoraQuiz Bot",
            url=get_ui_path(),
            js_api=api,
            width=840,
            height=660,
            min_size=(700, 560),
        )
        webview.start()
    except Exception:
        show_fatal_error(app_dir, traceback.format_exc())


if __name__ == "__main__":
    main()
