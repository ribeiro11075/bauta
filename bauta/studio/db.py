"""Studio's own state: its users, the identities they sign in with through an
identity provider, and their sessions, a record of everything
anyone did through it, and the runs it started. Nothing of bauta's -- run
state, history and manifests stay where `run` keeps them.

SQLite in the data directory by default; any database SQLAlchemy reaches
through BAUTA_STUDIO_DATABASE_URL otherwise.
"""
from __future__ import annotations

import datetime
import hashlib
from pathlib import Path
from typing import Any, Dict, Iterator, List, Optional, Tuple

from sqlalchemy import JSON, DateTime, ForeignKey, Integer, String, Text, create_engine, event, inspect
from sqlalchemy.engine import Engine
from sqlalchemy.orm import DeclarativeBase, Mapped, Session, mapped_column, relationship, sessionmaker

# ROLES: each role can do everything the ones before it can.
# VERSION_TABLE: Alembic's record of how far a database has been migrated,
#   named as Studio's tables are: the database may be shared with something
#   else using Alembic.
# BASELINE_REVISION: the revision that creates the tables as 0.2.7 did, without
#   Alembic.
VIEWER = 'viewer'
OPERATOR = 'operator'
ADMIN = 'admin'
ROLES = [VIEWER, OPERATOR, ADMIN]
MIGRATIONS_DIRECTORY = Path(__file__).parent / 'migrations'
VERSION_TABLE = 'studio_alembic_version'
BASELINE_REVISION = '0001'


def utcnow() -> datetime.datetime:
    """The time now, in UTC."""

    return datetime.datetime.now(datetime.timezone.utc)


def aware(value: datetime.datetime) -> datetime.datetime:
    """A stored time with its zone. SQLite gives back times without it, which
    would compare as local ones; they were stored in UTC.
    """

    return value if value.tzinfo else value.replace(tzinfo=datetime.timezone.utc)


def iso(value: Optional[datetime.datetime]) -> Optional[str]:
    """A stored time as ISO 8601 in UTC, which a browser reads as such."""

    return None if value is None else aware(value).isoformat()


class Base(DeclarativeBase):
    """The declarative base of Studio's tables."""

    pass


class User(Base):
    """A Studio account: its email, role, password hash, single sign-on
    identity and two-factor sign-in.
    """

    # passwordHash: None for someone who signs in only through the identity
    #   provider.
    # totpSecret, totpPendingSecret, totpLastStep: two-factor sign-in with an
    #   authenticator app: its secret, encrypted (see twofactor.py), once
    #   confirmed with a code; one being set up until then; and the last
    #   30-second step a code was taken for, so no code is taken twice.
    __tablename__ = 'studio_user'
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    email: Mapped[str] = mapped_column(String(320), unique=True, index=True)
    name: Mapped[str] = mapped_column(String(200), default='')
    passwordHash: Mapped[Optional[str]] = mapped_column(String(200), nullable=True)
    role: Mapped[str] = mapped_column(String(16), default=VIEWER)
    active: Mapped[bool] = mapped_column(default=True)
    createdAt: Mapped[datetime.datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    lastLoginAt: Mapped[Optional[datetime.datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    totpSecret: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    totpPendingSecret: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    totpLastStep: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    recoveryCodes: Mapped[List['RecoveryCode']] = relationship(back_populates='user', cascade='all, delete-orphan', passive_deletes=True)
    ssoIdentity: Mapped[Optional['SsoIdentity']] = relationship(back_populates='user', uselist=False, lazy='selectin', cascade='all, delete-orphan',
                                                                passive_deletes=True)

    def can(self, role: str) -> bool:
        """Whether the account is active and holds `role` or one above it."""

        return self.active and ROLES.index(self.role) >= ROLES.index(role)


    def asDict(self) -> Dict[str, Any]:
        """The account as the API gives it, never its password hash or secrets."""

        return {'id': self.id, 'email': self.email, 'name': self.name, 'role': self.role, 'active': self.active,
                'createdAt': iso(self.createdAt), 'lastLoginAt': iso(self.lastLoginAt), 'sso': self.ssoIdentity is not None,
                'passwordSet': self.passwordHash is not None, 'twoFactor': self.totpSecret is not None}


class SsoIdentity(Base):
    """Who a user is at the identity provider: its issuer and the subject it
    names them by, which, unlike an email, the provider never gives another
    person. One per user; an admin unlinks it when a person's identity at the
    provider was made again.

    The subject is kept as its SHA-256. Providers' subjects are case-sensitive,
    and MySQL's and SQL Server's usual collations compare text without case:
    two subjects differing only in case would be one person there. A digest
    in hex compares the same everywhere.
    """

    __tablename__ = 'studio_sso_identity'

    issuer: Mapped[str] = mapped_column(String(255), primary_key=True)
    subjectHash: Mapped[str] = mapped_column(String(64), primary_key=True)
    userId: Mapped[int] = mapped_column(ForeignKey('studio_user.id', ondelete='CASCADE'), unique=True)
    linkedAt: Mapped[datetime.datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    user: Mapped[User] = relationship(back_populates='ssoIdentity')

    @staticmethod
    def key(issuer: str, subject: str) -> Tuple[str, str]:
        """The primary key of `subject`'s identity at `issuer`."""

        return issuer, hashlib.sha256(subject.encode('utf-8')).hexdigest()


    @classmethod
    def of(cls, issuer: str, subject: str) -> 'SsoIdentity':
        """An identity for the provider's issuer and subject, the subject kept
        only as its hash.
        """

        issuer, subjectHash = cls.key(issuer, subject)

        return cls(issuer=issuer, subjectHash=subjectHash)


class RecoveryCode(Base):
    """A one-time code that stands in for an authenticator's, for someone who
    lost theirs. Only its SHA-256 is kept: it is random enough that a slow hash
    would add nothing.
    """

    __tablename__ = 'studio_recovery_code'

    codeHash: Mapped[str] = mapped_column(String(64), primary_key=True)
    userId: Mapped[int] = mapped_column(ForeignKey('studio_user.id', ondelete='CASCADE'), index=True)
    user: Mapped[User] = relationship(back_populates='recoveryCodes')


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
        """The activity log entry as the API gives it."""

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
        """The launched run as the API gives it."""

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
        """The check as the API gives it: its kind, when, by whom, and its result."""

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
    """An engine for Studio's database, foreign keys enforced on SQLite,
    migrated to the latest revision.
    """

    engine = create_engine(url, connect_args={'check_same_thread': False} if url.startswith('sqlite') else {})
    if url.startswith('sqlite'):
        @event.listens_for(engine, 'connect')
        def _foreignKeys(connection: Any, record: Any) -> None:
            connection.execute('PRAGMA foreign_keys = ON')

    migrate(engine)

    return engine


def migrate(engine: Engine) -> None:
    """Brings the database up to this version's tables, in place, keeping what
    it holds: creates them on first start, and applies whatever a release has
    added since. A database 0.2.7 made has its tables but no record of them,
    so it is marked as being at the baseline first.
    """

    from alembic import command
    from alembic.config import Config

    config = Config()
    config.set_main_option('script_location', str(MIGRATIONS_DIRECTORY))
    with engine.begin() as connection:
        config.attributes['connection'] = connection
        tables = set(inspect(connection).get_table_names())
        if VERSION_TABLE not in tables and User.__tablename__ in tables:
            command.stamp(config, BASELINE_REVISION)
        command.upgrade(config, 'head')


class Database:
    """Studio's database: a session per request, closed after it."""

    def __init__(self, url: str) -> None:
        """Studio's database at `url`, migrated, with sessions that keep their
        objects readable after a commit.
        """

        self.engine = makeEngine(url)
        self._sessions = sessionmaker(self.engine, expire_on_commit=False)


    def session(self) -> Iterator[Session]:
        """A session for one request, closed after it."""

        with self._sessions() as session:
            yield session


    def open(self) -> Session:
        """A session the caller closes, as a `with` block does."""

        return self._sessions()


    def close(self) -> None:
        """Closes every connection the database holds."""

        self.engine.dispose()
