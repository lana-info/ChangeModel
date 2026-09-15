import os
import sys
from pathlib import Path

import uvicorn

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from proxy.app import app, PORT  # noqa: E402


def main() -> None:
    port = int(os.environ.get("PROXY_PORT", str(PORT)))
    # PID-файл для кнопки «Остановить прокси» в GUI.
    pid_file = Path(__file__).resolve().parent.parent / "proxy" / "proxy.pid"
    try:
        pid_file.parent.mkdir(exist_ok=True)
        pid_file.write_text(str(os.getpid()), encoding="utf-8")
    except OSError:
        pass
    config = uvicorn.Config("proxy.app:app", host="127.0.0.1", port=port, log_level="info")
    server = uvicorn.Server(config)
    app.state.uvicorn_server = server
    server.run()


if __name__ == "__main__":
    main()