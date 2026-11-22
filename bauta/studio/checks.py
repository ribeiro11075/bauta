"""The checks that connect to the databases -- coverage, the connected audit,
discovery, references -- run when someone asks, or every night on Studio's
own schedule, each result kept with who or what ran it.

What a check found that the one before it didn't is what a reader needs
first: a table that appeared, a column a source gained, a reference that
broke. `whatsNew` compares the latest of each with the one before.
"""
from __future__ import annotations

import datetime
import logging
import threading
from typing import Any, Callable, Dict, List, Optional

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..configuration import isLake
from ..log.scrubbing import describeError
from ..project import Project
from .db import Activity, Check, Database, Setting, aware

# SCHEDULE: who ran a check the schedule ran.
# TICK_SECONDS: how often the schedule looks at the clock.
# CHECKS_PAGE: checks read from Studio's database at a time, looking for the
#   latest.
SCHEDULE_SETTING = 'checkSchedule'
KINDS = ['coverage', 'audit', 'discovery', 'references']
DEFAULT_SCHEDULE = {'enabled': False, 'time': '03:00', 'kinds': KINDS}
SCHEDULE = 'schedule'
TICK_SECONDS = 60.0
CHECKS_PAGE = 200
logger = logging.getLogger(__name__)


def saveCheck(session: Session, kind: str, email: str, result: Dict[str, Any]) -> Dict[str, Any]:
    """Stores a check's result, run by `email`, and returns it as the API gives it."""

    check = Check(kind=kind, email=email, result=result)
    session.add(check)
    session.flush()

    return check.asDict()


def latest(session: Session, kind: str, count: int = 1, where: Optional[Callable[[Check], bool]] = None) -> List[Check]:
    """The newest `count` checks of `kind`, newest first, of those `where`
    accepts. Read a page at a time, as far back as it takes: only the newest
    200 were read, so a job measured before 200 other measurements, or a
    database checked before 200 others, showed as never checked.
    """

    found: List[Check] = []
    before: Optional[int] = None
    while len(found) < count:
        query = select(Check).where(Check.kind == kind)
        if before is not None:
            query = query.where(Check.id < before)
        page = list(session.scalars(query.order_by(Check.id.desc()).limit(CHECKS_PAGE)))
        found.extend(check for check in page if where is None or where(check))
        if len(page) < CHECKS_PAGE:
            break
        before = page[-1].id

    return found[:count]


def latestDict(session: Session, kind: str) -> Optional[Dict[str, Any]]:
    """The latest check of `kind`, as the API gives it, or None."""

    found = latest(session, kind)

    return found[0].asDict() if found else None


# Running each ---------------------------------------------------------------

def sourceDatabases(project: Project) -> List[str]:
    """The databases the active jobs read from: what coverage is checked for."""

    return sorted({job.sourceConnection for job in project.jobsFile.jobs.values() if job.active and not isLake(project.connections[job.sourceConnection])})


def runCoverage(session: Session, project: Project, email: str, connection: Optional[str] = None, schema: Optional[str] = None) -> List[Dict[str, Any]]:
    """Coverage of `connection`, or of every database the jobs read from."""

    results = []
    for alias in ([connection] if connection else sourceDatabases(project)):
        report = project.coverage(connection=alias, schema=schema)
        results.append(saveCheck(session, 'coverage', email, {'report': report}))

    return results


def runAudit(session: Session, project: Project, email: str) -> Dict[str, Any]:
    """Runs the audit against the databases, and stores it."""

    return saveCheck(session, 'audit-connected', email, {'report': project.audit(connect=True), 'connected': True})


def runDiscovery(session: Session, project: Project, email: str) -> Dict[str, Any]:
    """Looks for columns the masked jobs' policies don't cover, and stores what
    it found.
    """

    from .discovery import findings

    return saveCheck(session, 'discovery', email, findings(project))


def runReferences(session: Session, project: Project, email: str) -> Dict[str, Any]:
    """Checks the targets' foreign keys for orphaned rows, and stores the report."""

    from ..review.references import referencesReport

    results, notes = project.verifyReferences()
    generatedAt = datetime.datetime.now(datetime.timezone.utc).isoformat(timespec='seconds')

    return saveCheck(session, 'references', email, {'report': referencesReport(results, generatedAt, notes)})


RUNNERS: Dict[str, Callable[[Session, Project, str], Any]] = {
    'coverage': runCoverage, 'audit': runAudit, 'discovery': runDiscovery, 'references': runReferences}


# What's new ---------------------------------------------------------------------

def _uncovered(check: Check) -> Dict[str, List[str]]:
    """The tables a coverage check found no job copying, each with the columns
    that looked personal.
    """

    return {entry['table']: entry.get('personalDataColumns', []) for entry in check.result['report']['tables'] if entry['state'] == 'uncovered'}


def _drifted(check: Check) -> Dict[str, List[str]]:
    """The jobs a discovery check found new columns in, each with those columns."""

    return {entry['job']: [added['column'] for added in entry['added']] for entry in check.result.get('jobs', [])}


def _broken(check: Check) -> Dict[str, int]:
    """Each reference with orphaned rows, as `table(columns) -> table`."""

    return {'{}.{}({}) -> {}'.format(entry['database'], entry['table'], ', '.join(entry['columns']), entry['referencedTable']): entry['orphans']
            for entry in check.result['report']['references'] if entry.get('orphans')}


def _ofDatabase(database: str) -> Callable[[Check], bool]:
    """A test that a coverage check is of `database`."""

    return lambda check: bool(check.result['report']['database'] == database)


def whatsNew(session: Session) -> List[Dict[str, Any]]:
    """What the latest of each check found that the one before it didn't, or
    everything it found when there is none before: tables no job covers,
    columns no policy names, references with orphaned rows.
    """

    news: List[Dict[str, Any]] = []

    databases = {check.result['report']['database'] for check in latest(session, 'coverage', 20)}
    for database in sorted(databases):
        checks = latest(session, 'coverage', 2, where=_ofDatabase(database))
        now = _uncovered(checks[0])
        before = _uncovered(checks[1]) if len(checks) > 1 else {}
        for table in sorted(set(now) - set(before)):
            news.append({'kind': 'coverage', 'database': database, 'table': table, 'personalDataColumns': now[table], 'at': checks[0].asDict()['at'],
                         'by': checks[0].email})

    checks = latest(session, 'discovery', 2)
    if checks:
        now, before = _drifted(checks[0]), _drifted(checks[1]) if len(checks) > 1 else {}
        for job, columns in sorted(now.items()):
            new = [column for column in columns if column not in before.get(job, [])]
            if new:
                news.append({'kind': 'discovery', 'job': job, 'columns': new, 'at': checks[0].asDict()['at'], 'by': checks[0].email})

    checks = latest(session, 'references', 1)
    if checks:
        for reference, orphans in sorted(_broken(checks[0]).items()):
            news.append({'kind': 'references', 'reference': reference, 'orphans': orphans, 'at': checks[0].asDict()['at'], 'by': checks[0].email})

    return news


# The schedule -------------------------------------------------------------------

def schedule(session: Session) -> Dict[str, Any]:
    """When the checks run on their own: `enabled`, a `time` of day, HH:MM in
    UTC, and which `kinds`.
    """

    found = session.get(Setting, SCHEDULE_SETTING)

    return dict(DEFAULT_SCHEDULE, **(found.value if found is not None and isinstance(found.value, dict) else {}))


def setSchedule(session: Session, enabled: bool, time: str, kinds: List[str]) -> Dict[str, Any]:
    """Stores the check schedule: whether it runs, its time of day in UTC as
    HH:MM, and the kinds of check it runs. Raises ValueError for a time or kind
    that isn't one.
    """

    try:
        hours, minutes = (int(part) for part in time.split(':'))
        if not (0 <= hours < 24 and 0 <= minutes < 60):
            raise ValueError
    except ValueError as error:
        raise ValueError('the time is HH:MM, in UTC') from error
    unknown = sorted(set(kinds) - set(KINDS))
    if unknown:
        raise ValueError('no such check: {}'.format(', '.join(unknown)))
    value = {'enabled': enabled, 'time': '{:02d}:{:02d}'.format(hours, minutes), 'kinds': [kind for kind in KINDS if kind in kinds]}
    found = session.get(Setting, SCHEDULE_SETTING)
    if found is None:
        session.add(Setting(name=SCHEDULE_SETTING, value=value))
    else:
        found.value = value

    return value


def _lastScheduledRun(session: Session) -> Optional[datetime.datetime]:
    """When the schedule last ran the checks, from the activity log, or None."""

    found = session.scalars(select(Activity).where(Activity.action == 'ran the scheduled checks').order_by(Activity.id.desc()).limit(1)).first()
    if found is None:
        return None

    return aware(found.at)


def due(session: Session, now: datetime.datetime) -> bool:
    """Whether today's time has passed and the checks haven't run since: so a
    Studio that was down at the time catches up when it starts.
    """

    setting = schedule(session)
    if not setting['enabled'] or not setting['kinds']:
        return False
    hours, minutes = (int(part) for part in setting['time'].split(':'))
    today = now.replace(hour=hours, minute=minutes, second=0, microsecond=0)
    last = _lastScheduledRun(session)

    return now >= today and (last is None or last < today)


def runScheduled(session: Session, project: Project) -> Dict[str, str]:
    """Each scheduled check, one failing not stopping the others; recorded
    as one activity with what became of each.
    """

    from .auth import record

    outcomes: Dict[str, str] = {}
    for kind in schedule(session)['kinds']:
        try:
            RUNNERS[kind](session, project, SCHEDULE)
            session.commit()
            outcomes[kind] = 'done'
        except Exception as error:
            session.rollback()
            described = describeError(error)
            logger.warning('The scheduled {} check failed: {}'.format(kind, described))
            outcomes[kind] = 'failed: {}'.format(described[:300])
    record(session, None, 'ran the scheduled checks', ', '.join(outcomes), **outcomes)
    session.commit()

    return outcomes


def scheduleLoop(database: Database, loadProject: Callable[[], Project], stop: threading.Event,
                 clock: Callable[[], datetime.datetime] = lambda: datetime.datetime.now(datetime.timezone.utc)) -> None:
    """Studio's schedule: once a minute, runs the checks if they are due."""

    while not stop.wait(TICK_SECONDS):
        try:
            with database.open() as session:
                if due(session, clock()):
                    runScheduled(session, loadProject())
        except Exception as error:
            logger.warning('The check schedule could not run: {}'.format(describeError(error)))
