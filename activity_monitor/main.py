import argparse
import asyncio
import os
from importlib import resources
from pathlib import Path

import uvicorn
from fastapi import FastAPI
from fastapi.concurrency import asynccontextmanager

from activity_monitor import api, db
from activity_monitor.config import AppConfig
from activity_monitor.log import TimestampAccessFormatter, TimestampFormatter, get_logger
from activity_monitor.sources import PushSource, build_sources


def parse_arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Activity Monitor API")
    parser.add_argument("--config", type=Path, default=Path("config.yaml"), help="Configuration file")
    parser.add_argument("--database", type=Path, default=None, help="SQLite database file")
    parser.add_argument("--port", type=int, default=None, help="API port")
    parser.add_argument(
        "--openapi", type=Path, default=None, metavar="FILE", help="Write OpenAPI schema to FILE and exit"
    )
    parser.add_argument(
        "--watch-windows",
        action="store_true",
        help="Run the desktop watcher in-process, writing events straight to the database",
    )
    return parser.parse_args()


def create_app(args: argparse.Namespace) -> FastAPI:
    config = AppConfig.load(args.config)

    if args.database:
        config.database = str(args.database)

    if args.port:
        config.api_port = args.port
    elif os.environ.get("ACTIVITY_MONITOR_PORT"):
        config.api_port = int(os.environ["ACTIVITY_MONITOR_PORT"])

    if getattr(args, "watch_windows", False):
        config.watch_windows = True

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        await db.init(config.database)
        app.state.sources = build_sources(config)

        watcher_stop = None
        watcher_thread = None

        if config.watch_windows:
            from activity_monitor.windows_watcher import start_database_watcher

            settings = config.source_settings("windows")
            provider = app.state.sources.get("windows") or PushSource("windows", {})
            watcher_stop, watcher_thread = start_database_watcher(
                provider,
                loop=asyncio.get_running_loop(),
                poll=float(settings.get("poll", 1.0)),
                idle_minutes=float(settings.get("idle_minutes", 2.0)),
                actor=str(settings.get("actor", "user")),
                verbose=bool(settings.get("verbose", False)),
            )

        try:
            yield
        finally:
            if watcher_stop is not None:
                watcher_stop.set()

            if watcher_thread is not None:
                watcher_thread.join(timeout=5)

            await db.dispose()

    app = FastAPI(title="Activity Monitor", version="0.1.0", lifespan=lifespan)
    app.state.config = config
    app.include_router(api.router)

    dev_dist = Path("frontend/dist")
    pkg_dist = Path(str(resources.files("activity_monitor") / "frontend" / "dist"))
    frontend_dir = dev_dist if dev_dist.is_dir() else pkg_dist

    if frontend_dir.is_dir():
        app.frontend("/", directory=frontend_dir)

    return app


def main() -> None:
    args = parse_arguments()
    app = create_app(args)

    if args.openapi:
        import json

        args.openapi.write_text(json.dumps(app.openapi(), indent=2))
        return

    log_config = uvicorn.config.LOGGING_CONFIG
    formatters = dict(log_config["formatters"])
    formatters["default"] = {
        "()": TimestampFormatter,
        "fmt": "%(asctime)s %(levelprefix)s %(message)s",
        "use_colors": None,
    }
    formatters["access"] = {
        "()": TimestampAccessFormatter,
        "fmt": '%(asctime)s %(levelprefix)s %(client_addr)s - "%(request_line)s" %(status_code)s',
        "use_colors": None,
    }
    log_config = {**log_config, "formatters": formatters}

    config = uvicorn.Config(
        app, host="0.0.0.0", port=app.state.config.api_port, timeout_graceful_shutdown=5, log_config=log_config
    )
    config.load_app()
    server = uvicorn.Server(config)

    get_logger().info("Activity Monitor: http://localhost:%s", config.port)
    get_logger().info("Swagger:          http://localhost:%s/docs", config.port)

    try:
        server.run()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
