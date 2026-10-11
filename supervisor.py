"""Launch the activity monitor backend and restart it whenever it exits.

- Ctrl+C (SIGINT) stops the launcher and the backend.
- SIGTERM/SIGBREAK are forwarded to the backend so it can stop gracefully;
  the launcher stays alive and restarts the backend.
"""

import os
import signal
import subprocess
import sys
import time
from pathlib import Path

BACKEND_DIR = Path(__file__).resolve().parent


def main() -> int:
    restart_delay = float(os.environ.get("RESTART_DELAY", "1"))
    backend: subprocess.Popen | None = None
    stop = False

    def forward_signal(signum, frame):
        print(f"[launcher] received signal {signum}; forwarding to backend...", flush=True)
        if backend is not None and backend.poll() is None:
            try:
                backend.send_signal(signum)
            except (OSError, ValueError):
                pass

    def stop_launcher(signum, frame):
        nonlocal stop
        stop = True
        print("[launcher] received stop signal; stopping backend...", flush=True)
        if backend is not None and backend.poll() is None:
            try:
                backend.send_signal(signal.SIGINT)
            except (OSError, ValueError):
                pass

    signal.signal(signal.SIGINT, stop_launcher)

    for sig in (signal.SIGTERM, signal.SIGBREAK):
        try:
            signal.signal(sig, forward_signal)
        except (ValueError, OSError, AttributeError):
            pass

    print(f"[launcher] backend dir: {BACKEND_DIR}", flush=True)

    while not stop:
        print("[launcher] starting backend...", flush=True)
        backend = subprocess.Popen([sys.executable, "-m", "activity_monitor.main", *sys.argv[1:]], cwd=BACKEND_DIR)

        try:
            status = backend.wait()
        except KeyboardInterrupt:
            stop = True
            break

        if stop:
            break

        print(f"[launcher] backend exited with status {status}; restarting in {restart_delay}s...", flush=True)
        time.sleep(restart_delay)

    if backend is not None and backend.poll() is None:
        backend.wait()

    print("[launcher] stopped.", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
