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
    parser.add_argument(
        "--config",
        type=Path,
        default=Path("config.yaml"),
        help="Configuration file (written with full defaults if missing)",
    )
    parser.add_argument(
        "--openapi", type=Path, default=None, metavar="FILE", help="Write OpenAPI schema to FILE and exit"
    )
    return parser.parse_args()


def create_app(config: AppConfig) -> FastAPI:
    @asynccontextmanager
    async def lifespan(app: FastAPI):
        await db.init(config.general.database)
        app.state.sources = build_sources()

        watcher_stop = None
        watcher_thread = None

        # `general.watcher` embeds the watcher (direct database ingest). Off
        # Windows there is no implementation; the setting is ignored silently so
        # the same config file works everywhere.
        if config.general.watcher and os.name == "nt":
            from activity_monitor.services.windows_watcher import start_database_watcher
            from activity_monitor.util.process_config import load_process_config

            provider = app.state.sources[USER_SOURCE]
            process_config = config.watcher.process_config or os.environ.get("ACTIVITY_MONITOR_PROCESS_CONFIG")
            watcher_stop, watcher_thread = start_database_watcher(
                provider,
                loop=asyncio.get_running_loop(),
                source=config.watcher.source,
                actor=config.watcher.actor,
                host=config.watcher.host,
                task=config.watcher.task,
                poll=config.watcher.poll,
                idle_minutes=config.watcher.idle_minutes,
                skew_seconds=config.watcher.skew_seconds,
                heartbeat=config.watcher.heartbeat_seconds,
                verbose=config.watcher.verbose,
                process_rules=load_process_config(process_config) if process_config else (),
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
    auth_deps = [Depends(require_auth)] if config.general.basic_auth_username else []

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
    config = AppConfig.load(args.config)
    app = create_app(config)

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

    general = config.general
    config_kwargs: dict = {
        "app": app,
        "host": "0.0.0.0",
        "port": general.api_port,
        "timeout_graceful_shutdown": 5,
        "log_config": log_config,
    }
    if general.ssl_enabled:
        if not general.ssl_certfile or not general.ssl_keyfile:
            raise SystemExit("SSL enabled but ssl_certfile and ssl_keyfile are required")
        import ssl

        config_kwargs["ssl_certfile"] = general.ssl_certfile
        config_kwargs["ssl_keyfile"] = general.ssl_keyfile
        if general.ssl_ca_certs:
            config_kwargs["ssl_ca_certs"] = general.ssl_ca_certs
        req = str(general.ssl_cert_reqs).lower()
        config_kwargs["ssl_cert_reqs"] = {
            "none": ssl.CERT_NONE,
            "optional": ssl.CERT_OPTIONAL,
            "required": ssl.CERT_REQUIRED,
        }.get(req, ssl.CERT_NONE)

    uvicorn_config = uvicorn.Config(**config_kwargs)
    uvicorn_config.load_app()
    server = uvicorn.Server(uvicorn_config)

    scheme = "https" if general.ssl_enabled else "http"
    get_logger().info("Activity Monitor: %s://localhost:%s", scheme, uvicorn_config.port)
    get_logger().info("Swagger:          %s://localhost:%s/docs", scheme, uvicorn_config.port)

    try:
        server.run()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
