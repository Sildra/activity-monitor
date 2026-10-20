"""Program lifecycle: closing stale programs and finding the active one."""

from __future__ import annotations

from datetime import datetime, timedelta

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from activity_monitor.model.program import Program


async def expire_stale(session: AsyncSession, now: datetime, stale: timedelta) -> None:
    cutoff = now - stale
    stale_rows = list(
        (await session.execute(select(Program).where(Program.ended_at.is_(None), Program.last_seen_at < cutoff)))
        .scalars()
        .all()
    )

    for program in stale_rows:
        program.ended_at = program.last_seen_at

    if stale_rows:
        await session.commit()


async def find_active_program(session: AsyncSession, program_id: str) -> Program | None:
    return (
        (
            await session.execute(
                select(Program)
                .where(Program.program_id == program_id, Program.ended_at.is_(None))
                .order_by(Program.started_at.desc())
            )
        )
        .scalars()
        .first()
    )
