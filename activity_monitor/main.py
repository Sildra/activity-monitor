import argparse
import asyncio
import os
from importlib import resources
from pathlib import Path

import uvicorn
from fastapi import Depends, FastAPI
from fastapi.concurrency import asynccontextmanager

from activity_monitor.api.deps import require_auth
from activity_monitor.api.events import router as events_router
from activity_monitor.api.programs import router as programs_router
from activity_monitor.api.sources import router as sources_router
from activity_monitor.api.timelines import router as timelines_router
from activity_monitor.log import TimestampAccessFormatter, TimestampFormatter, get_logger
from activity_monitor.services import db
from activity_monitor.services.config import AppConfig
from activity_monitor.services.sources import USER_SOURCE, build_sources


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
    parser.add_argument(
        "--tray",
        action="store_true",
        help="Show a tray icon while the server runs (its Exit entry stops the server; Windows only)",
    )
    parser.add_argument("--ssl", action="store_true", help="Enable HTTPS")
    parser.add_argument("--ssl-certfile", type=Path, default=None, help="Path to SSL certificate file")
    parser.add_argument("--ssl-keyfile", type=Path, default=None, help="Path to SSL private key file")
    parser.add_argument("--ssl-ca-certs", type=Path, default=None, help="Path to CA certificate for client auth")
    parser.add_argument("--ssl-cert-reqs", default=None, help="Client cert requirement: none|optional|required")
    parser.add_argument("--basic-auth-username", default=None, help="Require HTTP Basic auth with this username")
    parser.add_argument("--basic-auth-password", default=None, help="HTTP Basic auth password")
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

    if getattr(args, "ssl", False):
        config.ssl_enabled = True
    if getattr(args, "ssl_certfile", None) is not None:
        config.ssl_certfile = str(args.ssl_certfile)
    if getattr(args, "ssl_keyfile", None) is not None:
        config.ssl_keyfile = str(args.ssl_keyfile)
    if getattr(args, "ssl_ca_certs", None) is not None:
        config.ssl_ca_certs = str(args.ssl_ca_certs)
    if getattr(args, "ssl_cert_reqs", None) is not None:
        config.ssl_cert_reqs = args.ssl_cert_reqs

    if getattr(args, "basic_auth_username", None) is not None:
        config.basic_auth_username = args.basic_auth_username
    if getattr(args, "basic_auth_password", None) is not None:
        config.basic_auth_password = args.basic_auth_password

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        await db.init(config.database)
        app.state.sources = build_sources()

        watcher_stop = None
        watcher_thread = None

        if config.watch_windows:
            from activity_monitor.services.windows_watcher import start_database_watcher
            from activity_monitor.util.process_config import load_process_config

            provider = app.state.sources[USER_SOURCE]
            process_config = config.process_config or os.environ.get("ACTIVITY_MONITOR_PROCESS_CONFIG")
            # The embedded watcher runs on its defaults (poll, idle minutes, skew,
            # heartbeat); the standalone watcher exposes them as flags.
            watcher_stop, watcher_thread = start_database_watcher(
                provider,
                loop=asyncio.get_running_loop(),
                process_rules=load_process_config(process_config) if process_config else (),
            )

        # The tray icon only exists with --tray (main() puts it on the app); it
        # turns green only once startup succeeded, so a failing init (or watcher
        # start) above leaves it gray until the process goes away.
        tray = getattr(app.state, "tray", None)

        if tray is not None:
            tray.set_state(True)

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
    auth_deps = [Depends(require_auth)] if config.basic_auth_username else []

    # Include order matches the route order the single router used to register,
    # so the OpenAPI path list stays identical.
    for router in (sources_router, programs_router, timelines_router, events_router):
        app.include_router(router, dependencies=auth_deps)

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

    cfg = app.state.config
    config_kwargs: dict = {
        "app": app,
        "host": "0.0.0.0",
        "port": cfg.api_port,
        "timeout_graceful_shutdown": 5,
        "log_config": log_config,
    }
    if cfg.ssl_enabled:
        if not cfg.ssl_certfile or not cfg.ssl_keyfile:
            raise SystemExit("SSL enabled but ssl_certfile and ssl_keyfile are required")
        import ssl

        config_kwargs["ssl_certfile"] = cfg.ssl_certfile
        config_kwargs["ssl_keyfile"] = cfg.ssl_keyfile
        if cfg.ssl_ca_certs:
            config_kwargs["ssl_ca_certs"] = cfg.ssl_ca_certs
        req = str(cfg.ssl_cert_reqs).lower()
        config_kwargs["ssl_cert_reqs"] = {
            "none": ssl.CERT_NONE,
            "optional": ssl.CERT_OPTIONAL,
            "required": ssl.CERT_REQUIRED,
        }.get(req, ssl.CERT_NONE)

    config = uvicorn.Config(**config_kwargs)
    config.load_app()
    server = uvicorn.Server(config)

    scheme = "https" if cfg.ssl_enabled else "http"
    get_logger().info("Activity Monitor: %s://localhost:%s", scheme, config.port)
    get_logger().info("Swagger:          %s://localhost:%s/docs", scheme, config.port)

    tray = None

    if args.tray:
        if os.name != "nt":
            raise SystemExit("--tray is only supported on Windows")

        from activity_monitor.util.tray import TrayIcon

        def stop_server() -> None:
            # uvicorn's graceful shutdown from another thread; it also stops an
            # embedded watcher through the lifespan's cleanup.
            server.should_exit = True

        tray = TrayIcon(f"Activity Monitor — localhost:{config.port}", on_exit=stop_server)
        tray.start()
        app.state.tray = tray

    try:
        server.run()
    except KeyboardInterrupt:
        pass
    finally:
        if tray is not None:
            tray.stop()


if __name__ == "__main__":
    main()
