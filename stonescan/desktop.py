"""Desktop launcher for the packaged (frozen) Stone Scanner app.

Double-clicking the exe starts the local server and opens the browser. Running it
with `--refresh` performs a crawl instead. All writable state (the database,
the editable supplier list) lives in a `data/` folder next to the exe; the bundle
itself is read-only. The bundled Chromium is located via PLAYWRIGHT_BROWSERS_PATH.
"""

from __future__ import annotations

import os
import shutil
import socket
import sys
import threading
import time
import traceback
import webbrowser
from pathlib import Path

_LOG: Path | None = None


def _log(msg: str) -> None:
    """Append a line to data/app.log (the packaged app has no console)."""
    try:
        line = f"{time.strftime('%Y-%m-%d %H:%M:%S')}  {msg}\n"
        if _LOG:
            with open(_LOG, "a", encoding="utf-8") as f:
                f.write(line)
    except Exception:
        pass
    print(msg)


def _frozen() -> bool:
    return bool(getattr(sys, "frozen", False))


def app_dir() -> Path:
    """Folder containing the exe (frozen) or the project root (dev)."""
    if _frozen():
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent.parent


def bundle_dir() -> Path:
    """Folder holding bundled read-only resources."""
    if _frozen():
        return Path(getattr(sys, "_MEIPASS"))
    return Path(__file__).resolve().parent.parent


def _seed(dest: Path, *candidates: Path) -> None:
    if dest.exists():
        return
    for c in candidates:
        if c.exists():
            shutil.copy2(c, dest)
            return


def setup_env() -> Path:
    """Prepare the writable data dir + environment; return the data dir."""
    base = app_dir()
    data = base / "data"
    data.mkdir(parents=True, exist_ok=True)

    db_path = data / "stonescan.db"
    _seed(db_path, bundle_dir() / "seed" / "stonescan.db", bundle_dir() / "stonescan.db")
    os.environ["STONESCAN_DB"] = str(db_path)

    sup = data / "suppliers.json"
    _seed(sup, bundle_dir() / "seed" / "suppliers.json", bundle_dir() / "suppliers.json")
    os.environ["STONESCAN_SUPPLIERS"] = str(sup)

    # Map-pin overrides: user-editable, so it lives in the writable data dir too.
    locs = data / "locations.json"
    _seed(locs, bundle_dir() / "seed" / "locations.json", bundle_dir() / "locations.json")
    os.environ["STONESCAN_LOCATIONS"] = str(locs)

    browsers = base / "browsers"
    if browsers.exists():
        os.environ["PLAYWRIGHT_BROWSERS_PATH"] = str(browsers)

    global _LOG
    _LOG = data / "app.log"
    return data


def _free_port(preferred: int = 8000) -> int:
    for p in (preferred, 8001, 8010, 8080, 8765, 0):
        try:
            s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            s.bind(("127.0.0.1", p))
            port = s.getsockname()[1]
            s.close()
            return port
        except OSError:
            continue
    return preferred


def _wait_until_up(port: int, timeout: float = 20.0) -> bool:
    """Poll the local server until it accepts connections."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            s = socket.create_connection(("127.0.0.1", port), timeout=1)
            s.close()
            return True
        except OSError:
            time.sleep(0.2)
    return False


def _start_server(port: int):
    """Run uvicorn in a background thread (no signal handlers off main thread)."""
    import uvicorn
    from stonescan.web.app import app

    config = uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning", log_config=None)
    server = uvicorn.Server(config)
    server.install_signal_handlers = lambda: None  # off-main-thread safe
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    return server


def run_server() -> None:
    """Launch as a native desktop window (no console, no external browser)."""
    setup_env()
    port = _free_port()
    url = f"http://127.0.0.1:{port}/"
    _log(f"starting server on {url}")
    _start_server(port)
    up = _wait_until_up(port)
    _log(f"server up={up}")

    try:
        import webview
        _log(f"pywebview loaded; creating window (gui={getattr(webview, 'guilib', None)})")
        webview.create_window("Stone Scanner", url, width=1320, height=880, min_size=(900, 600))
        webview.start()  # blocks until the window is closed, then the app exits
        _log("webview.start() returned (window closed)")
    except Exception:
        _log("WEBVIEW FAILED:\n" + traceback.format_exc())
        # Last-resort fallback (e.g. no WebView2 runtime): use the default browser.
        webbrowser.open(url)
        while True:
            time.sleep(3600)


def run_refresh(with_slabs: bool = True, do_discover: bool = False) -> None:
    setup_env()
    import asyncio
    from stonescan import discover as disc
    from stonescan.ingest import run as ingest_run

    if do_discover:
        print("Discovering public catalogs...")
        disc.merge_discovered(disc.discover_hosts())
    hosts = [s["host"] for s in disc.load_suppliers()]
    print(f"Refreshing {len(hosts)} catalog(s)"
          f"{' with slab galleries' if with_slabs else ''}...")
    asyncio.run(ingest_run(
        hosts, concurrency=4, delay=1.0, headless=True,
        db_path=os.environ["STONESCAN_DB"], with_slabs=with_slabs,
    ))


def _ensure_std_streams() -> None:
    """A windowed (no-console) exe has sys.stdout/stderr == None; some libraries
    (e.g. uvicorn's log formatter) call .isatty() on them. Give them a real sink."""
    if sys.stdout is None or sys.stderr is None:
        try:
            devnull = open(os.devnull, "w")
        except Exception:
            return
        if sys.stdout is None:
            sys.stdout = devnull
        if sys.stderr is None:
            sys.stderr = devnull


def main() -> None:
    _ensure_std_streams()
    args = set(a.lower() for a in sys.argv[1:])
    try:
        if {"--refresh", "--crawl"} & args:
            run_refresh(with_slabs=("--no-slabs" not in args), do_discover=("--discover" in args))
        else:
            run_server()
    except Exception:
        _log("FATAL:\n" + traceback.format_exc())
        raise


if __name__ == "__main__":
    main()
