"""
PostgreSQL database models for production scaling.
Requires: pip install sqlalchemy[asyncio] asyncpg

Enable by setting DATABASE_URL in .env:
  DATABASE_URL="postgresql+asyncpg://user:pass@localhost:5432/renewal_bot"
"""

import os
from datetime import datetime
from typing import Optional

try:
    from sqlalchemy.ext.asyncio import create_async_engine, AsyncSession, async_sessionmaker
    from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column
    from sqlalchemy import String, Integer, Text, BigInteger, select, func
    HAS_SQLALCHEMY = True
except ImportError:
    HAS_SQLALCHEMY = False

DATABASE_URL = os.getenv("DATABASE_URL")
engine = None
async_session_maker = None


class Base(DeclarativeBase):
    pass


class CallLog(Base):
    __tablename__ = "call_logs"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    timestamp: Mapped[str] = mapped_column(String(32), default=lambda: datetime.now().isoformat())
    customer_name: Mapped[Optional[str]] = mapped_column(String(255))
    mobile_number: Mapped[Optional[str]] = mapped_column(String(20), index=True)
    policy_number: Mapped[Optional[str]] = mapped_column(String(50))
    call_status: Mapped[Optional[str]] = mapped_column(String(20))
    duration: Mapped[Optional[int]] = mapped_column(Integer, default=0)
    disposition: Mapped[Optional[str]] = mapped_column(String(50))
    promise_to_pay_date: Mapped[Optional[str]] = mapped_column(String(20))
    concern_category: Mapped[Optional[str]] = mapped_column(String(100))
    concern_notes: Mapped[Optional[str]] = mapped_column(Text)
    alt_number: Mapped[Optional[str]] = mapped_column(String(20))
    detected_language: Mapped[Optional[str]] = mapped_column(String(20))
    sentiment: Mapped[Optional[str]] = mapped_column(String(20))
    partial_amount: Mapped[Optional[str]] = mapped_column(String(20))
    emi_option: Mapped[Optional[str]] = mapped_column(String(50))
    call_back_time: Mapped[Optional[str]] = mapped_column(String(50))
    transcript: Mapped[Optional[str]] = mapped_column(Text)
    recording_url: Mapped[Optional[str]] = mapped_column(String(500))


async def init_db():
    """Initialize the database. Falls back silently if PostgreSQL not configured."""
    global engine, async_session_maker
    if not DATABASE_URL or not HAS_SQLALCHEMY:
        return
    engine = create_async_engine(DATABASE_URL, echo=False)
    async_session_maker = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)


async def get_session() -> Optional[AsyncSession]:
    if async_session_maker is None:
        return None
    async with async_session_maker() as session:
        yield session


async def save_call_log(**kwargs) -> Optional[int]:
    """Insert a call log into PostgreSQL. Returns the row ID or None."""
    if async_session_maker is None:
        return None
    async with async_session_maker() as session:
        entry = CallLog(**{k: v for k, v in kwargs.items() if hasattr(CallLog, k)})
        session.add(entry)
        await session.commit()
        await session.refresh(entry)
        return entry.id


async def get_recent_calls(limit: int = 50) -> list:
    """Fetch recent call logs from PostgreSQL."""
    if async_session_maker is None:
        return []
    async with async_session_maker() as session:
        result = await session.execute(
            select(CallLog).order_by(CallLog.id.desc()).limit(limit)
        )
        return result.scalars().all()


async def get_pending_follow_ups() -> list:
    """Fetch calls with PTP dates that haven't been actioned."""
    if async_session_maker is None:
        return []
    async with async_session_maker() as session:
        today = datetime.now().strftime("%Y-%m-%d")
        result = await session.execute(
            select(CallLog).where(
                CallLog.disposition == "Promise to Pay",
                CallLog.promise_to_pay_date >= today,
            ).order_by(CallLog.promise_to_pay_date)
        )
        return result.scalars().all()


async def close_db():
    """Close the database connection pool."""
    global engine
    if engine:
        await engine.dispose()
