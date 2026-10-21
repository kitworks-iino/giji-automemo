from __future__ import annotations

import argparse
import os
import secrets
import socket
import sys
import threading
import time
import webbrowser
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from .store import Store, default_data_dir


def acquire_lock(data_dir: Path):
    data_dir.mkdir(parents=True, exist_ok=True)
    handle = open(data_dir / ".instance.lock", "a+b")
    try:
        if os.name == "nt":
            import msvcrt
            handle.seek(0)
            handle.write(b"0")
            handle.flush()
            handle.seek(0)
            msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        handle.close()
        raise RuntimeError("gijiはすでに起動しています。既存の画面を開くか、先に終了してください。") from None
    return handle


def main():
    if sys.argv[1:] == ["--teams-uia-calendar-worker"]:
        from .teams_calendar_uia import worker_main
        raise SystemExit(worker_main())
    if sys.argv[1:2] == ["--teams-connection-cleanup"]:
        from .teams_connection import cleanup_main
        raise SystemExit(cleanup_main(sys.argv[2:]))
    # The isolated helper must run COM on its own main STA thread. Frozen builds
    # use this entry point without starting a second app or opening the browser.
    if sys.argv[1:] == ["--calendar-worker"]:
        from .desktop_calendar import worker_main
        raise SystemExit(worker_main())
    parser = argparse.ArgumentParser(description="giji — Windows 11 Teams meeting notes")
    parser.add_argument("--data-dir", type=Path, default=default_data_dir())
    parser.add_argument("--port", type=int, default=0, help="Loopback port; 0 chooses a free port")
    parser.add_argument("--no-browser", action="store_true")
    parser.add_argument("--demo", action="store_true", help="Insert explicitly labelled sample content")
    args = parser.parse_args()
    from .runtime_dependencies import ProhibitedDependencyError, ensure_no_prohibited_dependencies

    try:
        ensure_no_prohibited_dependencies()
    except ProhibitedDependencyError as exc:
        parser.exit(1, f"{exc}\n")
    try:
        lock = acquire_lock(args.data_dir)
    except RuntimeError as exc:
        launch_file = args.data_dir / "launch-url.txt"
        if not args.no_browser and launch_file.is_file():
            existing = launch_file.read_text(encoding="utf-8")
            parsed = urlparse(existing)
            if (parsed.scheme == "http" and parsed.hostname == "127.0.0.1"
                    and parsed.path == "/" and parse_qs(parsed.fragment).get("token")):
                webbrowser.open(existing)
                return
        parser.exit(1, f"{exc}\n")
    from .api import create_app
    from .service import GijiService
    import uvicorn

    store = Store(args.data_dir)
    service = GijiService(store)
    if args.demo:
        service.seed_demo()
    token = secrets.token_urlsafe(32)
    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    if os.name == "nt":
        listener.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
    listener.bind(("127.0.0.1", args.port))
    port = listener.getsockname()[1]
    listener.listen(128)
    app = create_app(service, token, port)
    url = f"http://127.0.0.1:{port}/#token={token}"
    # Keep the launch URL local so starting giji again can reopen a closed tab.
    launch = args.data_dir / "launch-url.txt"
    launch.write_text(url, encoding="utf-8")
    try:
        launch.chmod(0o600)
    except OSError:
        pass
    print(f"giji running on http://127.0.0.1:{port} (Ctrl+C to quit)")
    if not args.no_browser:
        def open_ui():
            time.sleep(1)
            webbrowser.open(url)
        threading.Thread(target=open_ui, daemon=True).start()
    else:
        # Do not log the token.
        print(f"Local launch URL saved to {launch}")
    try:
        uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning",
                                    access_log=False, timeout_graceful_shutdown=10)).run(sockets=[listener])
    finally:
        listener.close()
        (args.data_dir / "launch-url.txt").unlink(missing_ok=True)
        lock.close()


if __name__ == "__main__":
    main()
