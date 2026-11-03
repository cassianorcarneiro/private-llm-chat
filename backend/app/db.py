"""Persistence layer: conversations, messages and the model catalog.

Conversation history is domain data that the UI lists, renames and deletes,
so it lives in plain relational tables instead of LangGraph's checkpointer
(which stores opaque per-step graph state and is not designed to be queried).
"""

import uuid
from datetime import datetime, timezone

from sqlalchemy import JSON, BigInteger, DateTime, ForeignKey, Integer, String, Text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

from .config import settings


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def new_id() -> str:
    return uuid.uuid4().hex


class Base(DeclarativeBase):
    pass


class Conversation(Base):
    __tablename__ = "conversations"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    title: Mapped[str] = mapped_column(String(200), default="New chat")
    model: Mapped[str | None] = mapped_column(String(300), nullable=True)
    # Rolling summary of messages that no longer fit in the context window.
    summary: Mapped[str] = mapped_column(Text, default="")
    # Messages with seq < summary_upto are represented only by `summary`.
    summary_upto: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class Message(Base):
    __tablename__ = "messages"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    conversation_id: Mapped[str] = mapped_column(
        ForeignKey("conversations.id", ondelete="CASCADE"), index=True
    )
    seq: Mapped[int] = mapped_column(Integer)
    role: Mapped[str] = mapped_column(String(16))
    content: Mapped[str] = mapped_column(Text, default="")
    thinking: Mapped[str] = mapped_column(Text, default="")
    model: Mapped[str | None] = mapped_column(String(300), nullable=True)
    # none_as_null: store SQL NULL rather than the JSON literal 'null'.
    sources: Mapped[list | None] = mapped_column(JSON(none_as_null=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class ModelEntry(Base):
    __tablename__ = "model_catalog"

    name: Mapped[str] = mapped_column(String(300), primary_key=True)
    digest: Mapped[str] = mapped_column(String(100), default="")
    size_bytes: Mapped[int] = mapped_column(BigInteger, default=0)
    family: Mapped[str] = mapped_column(String(100), default="")
    parameter_size: Mapped[str] = mapped_column(String(50), default="")
    quantization: Mapped[str] = mapped_column(String(50), default="")
    context_length: Mapped[int | None] = mapped_column(Integer, nullable=True)
    capabilities: Mapped[list] = mapped_column(JSON, default=list)
    description: Mapped[str] = mapped_column(Text, default="")
    description_source: Mapped[str] = mapped_column(String(500), default="")
    status: Mapped[str] = mapped_column(String(16), default="pending")
    error: Mapped[str] = mapped_column(Text, default="")
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


engine = create_async_engine(settings.database_url, pool_pre_ping=True)
SessionLocal = async_sessionmaker(engine, expire_on_commit=False)


async def init_db() -> None:
    # create_all is idempotent. For schema changes, introduce Alembic.
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
