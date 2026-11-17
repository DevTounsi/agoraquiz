from pathlib import Path
import queue
import sys
import threading
import webview

from agora_bot import (
    DEFAULT_BASE_MODEL,
    DEFAULT_EFFORT,
    DEFAULT_MODEL,
    DEFAULT_SUCCESS_RATE,
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
        return {
            "config": read_config_raw(self.app_dir),
            "models": fetch_grouped_models(),
            "default_base_model": DEFAULT_BASE_MODEL,
            "default_effort": DEFAULT_EFFORT,
            "default_success_rate": DEFAULT_SUCCESS_RATE,
        }

    def start(self, opts: dict) -> dict:
        if self._running:
            return {"ok": False, "error": "Le bot est déjà en cours d'exécution."}

        cfg = {
            "username": opts.get("username", "").strip(),
            "password": opts.get("password", ""),
            "quiz_name": opts.get("quiz_name", "").strip(),
        }
        save_config(self.app_dir, cfg)

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
                    success_rate=int(opts.get("success_rate", DEFAULT_SUCCESS_RATE)),
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


if __name__ == "__main__":
    main()
