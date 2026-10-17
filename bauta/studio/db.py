"""Studio's own state: its users and their sessions, a record of everything
anyone did through it, and the runs it started. Nothing of bauta's -- run
state, history and manifests stay where `run` keeps them.

SQLite in the data directory by default; any database SQLAlchemy reaches
through BAUTA_STUDIO_DATABASE_URL otherwise.
"""
from __future__ import annotations

import datetime
from typing import Any, Dict, Iterator, List, Optional

from sqlalchemy import JSON, DateTime, ForeignKey, Integer, String, Text, create_engine, event
from sqlalchemy.engine import Engine
from sqlalchemy.orm import DeclarativeBase, Mapped, Session, mapped_column, sessionmaker

VIEWER = 'viewer'
OPERATOR = 'operator'
ADMIN = 'admin'
# Each role can do everything the ones before it can.
ROLES = [VIEWER, OPERATOR, ADMIN]


def utcnow() -> datetime.datetime:

    return datetime.datetime.now(datetime.timezone.utc)


def iso(value: Optional[datetime.datetime]) -> Optional[str]:
    """A stored time as ISO 8601 in UTC. SQLite gives back times without their
    zone, which a browser would read as its own; they were stored in UTC.
    """

    if value is None:
        return None

    return (value if value.tzinfo else value.replace(tzinfo=datetime.timezone.utc)).isoformat()


class Base(DeclarativeBase):
    pass


class User(Base):
    __tablename__ = 'studio_user'

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    email: Mapped[str] = mapped_column(String(320), unique=True, index=True)
    name: Mapped[str] = mapped_column(String(200), default='')
    passwordHash: Mapped[str] = mapped_column(String(200))
    role: Mapped[str] = mapped_column(String(16), default=VIEWER)
    active: Mapped[bool] = mapped_column(default=True)
    createdAt: Mapped[datetime.datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    lastLoginAt: Mapped[Optional[datetime.datetime]] = mapped_column(DateTime(timezone=True), nullable=True)

    def can(self, role: str) -> bool:

        return self.active and ROLES.index(self.role) >= ROLES.index(role)


    def asDict(self) -> Dict[str, Any]:

        return {'id': self.id, 'email': self.email, 'name': self.name, 'role': self.role, 'active': self.active,
                'createdAt': iso(self.createdAt), 'lastLoginAt': iso(self.lastLoginAt)}


class UserSession(Base):
    """A signed-in browser. Only the token's SHA-256 is kept, so a copy of the
    database signs no one in.
    """

    __tablename__ = 'studio_session'

    tokenHash: Mapped[str] = mapped_column(String(64), primary_key=True)
    userId: Mapped[int] = mapped_column(ForeignKey('studio_user.id', ondelete='CASCADE'), index=True)
    createdAt: Mapped[datetime.datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    expiresAt: Mapped[datetime.datetime] = mapped_column(DateTime(timezone=True))


class Activity(Base):
    """One thing someone did: signed in, started a run, changed a file, added
    a user. `detail` holds what it needs to be read later -- a file's diff, a
    run's jobs -- and never a password or a value from a table.
    """

    __tablename__ = 'studio_activity'

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    at: Mapped[datetime.datetime] = mapped_column(DateTime(timezone=True), default=utcnow, index=True)
    userId: Mapped[Optional[int]] = mapped_column(ForeignKey('studio_user.id', ondelete='SET NULL'), nullable=True)
    email: Mapped[str] = mapped_column(String(320), default='')
    action: Mapped[str] = mapped_column(String(64))
    target: Mapped[str] = mapped_column(String(500), default='')
    detail: Mapped[Dict[str, Any]] = mapped_column(JSON, default=dict)

    def asDict(self) -> Dict[str, Any]:

        return {'id': self.id, 'at': iso(self.at), 'email': self.email, 'action': self.action, 'target': self.target, 'detail': self.detail}


class LaunchedRun(Base):
    """A `bauta run` Studio started. `runId` is filled in once the run's live
    status names it, matched by process id.
    """

    __tablename__ = 'studio_launched_run'

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    userId: Mapped[Optional[int]] = mapped_column(ForeignKey('studio_user.id', ondelete='SET NULL'), nullable=True)
    email: Mapped[str] = mapped_column(String(320), default='')
    startedAt: Mapped[datetime.datetime] = mapped_column(DateTime(timezone=True), default=utcnow, index=True)
    finishedAt: Mapped[Optional[datetime.datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    jobs: Mapped[List[str]] = mapped_column(JSON, default=list)
    options: Mapped[Dict[str, Any]] = mapped_column(JSON, default=dict)
    pid: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    exitCode: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    runId: Mapped[Optional[str]] = mapped_column(String(36), nullable=True, index=True)
    logPath: Mapped[str] = mapped_column(Text, default='')

    def asDict(self) -> Dict[str, Any]:

        return {'id': self.id, 'email': self.email, 'startedAt': iso(self.startedAt), 'finishedAt': iso(self.finishedAt), 'jobs': self.jobs,
                'options': self.options,
                'pid': self.pid, 'exitCode': self.exitCode, 'runId': self.runId}


class Check(Base):
    """The result of a check someone ran -- coverage, a connected audit,
    discovery -- kept so everyone sees the latest, after a restart too.
    """

    __tablename__ = 'studio_check'

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    kind: Mapped[str] = mapped_column(String(32), index=True)
    at: Mapped[datetime.datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    email: Mapped[str] = mapped_column(String(320), default='')
    result: Mapped[Dict[str, Any]] = mapped_column(JSON, default=dict)

    def asDict(self) -> Dict[str, Any]:

        return {'id': self.id, 'kind': self.kind, 'at': iso(self.at), 'by': self.email, **self.result}


class Setting(Base):
    """One of Studio's own settings, changed by an admin: see settings.py."""

    __tablename__ = 'studio_setting'

    name: Mapped[str] = mapped_column(String(64), primary_key=True)
    value: Mapped[Any] = mapped_column(JSON)


class ArchivedManifest(Base):
    """A manifest as `run` wrote it, kept by run: a manifest file is replaced
    by each run, and a run's evidence needs the one it wrote.
    """

    __tablename__ = 'studio_manifest'

    runId: Mapped[str] = mapped_column(String(64), primary_key=True)
    archivedAt: Mapped[datetime.datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    manifest: Mapped[Dict[str, Any]] = mapped_column(JSON)


def makeEngine(url: str) -> Engine:

    engine = create_engine(url, connect_args={'check_same_thread': False} if url.startswith('sqlite') else {})
    if url.startswith('sqlite'):
        @event.listens_for(engine, 'connect')
        def _foreignKeys(connection: Any, record: Any) -> None:
            connection.execute('PRAGMA foreign_keys = ON')

    # Tables made on first start. Before the first release this becomes
    # Alembic migrations, so an upgrade changes a customer's tables in place.
    Base.metadata.create_all(engine)

    return engine


class Database:
    """Studio's database: a session per request, closed after it."""

    def __init__(self, url: str) -> None:
        self.engine = makeEngine(url)
        self._sessions = sessionmaker(self.engine, expire_on_commit=False)


    def session(self) -> Iterator[Session]:

        with self._sessions() as session:
            yield session


    def open(self) -> Session:

        return self._sessions()


    def close(self) -> None:
        """Closes every connection the database holds."""

        self.engine.dispose()
