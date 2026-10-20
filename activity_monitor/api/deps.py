"""Shared request dependencies for the API routers."""

from __future__ import annotations

import secrets
from datetime import timedelta
from typing import Annotated

from fastapi import Depends, HTTPException, Request, status
from fastapi.security import HTTPBasic, HTTPBasicCredentials
from sqlalchemy.ext.asyncio import AsyncSession

from activity_monitor.services.db import get_session

SessionDep = Annotated[AsyncSession, Depends(get_session)]

_security = HTTPBasic(auto_error=False)


def require_auth(request: Request, credentials: Annotated[HTTPBasicCredentials | None, Depends(_security)]) -> None:
    """Enforce HTTP Basic auth when the backend is configured with credentials.

    Mounted on the API router (see `create_app`), so it only runs when
    `general.basic_auth_username` is set; the static frontend and the OpenAPI
    docs stay public.
    """
    config = request.app.state.config
    username = config.basic_auth_username

    if not username:
        return

    if credentials is None or not (
        secrets.compare_digest(credentials.username, username)
        and secrets.compare_digest(credentials.password, config.basic_auth_password or "")
    ):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid credentials",
            headers={"WWW-Authenticate": "Basic"},
        )


def stale_after(request: Request) -> timedelta:
    return timedelta(seconds=request.app.state.config.stale_after_seconds)
