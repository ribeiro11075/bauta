"""Where each job stands on its schedule, and which have gone stale: an
active job that hasn't completed within the hours set for it, whether its
runs failed, were skipped, or never started at all.

The thresholds are Studio's own, set by an admin: one for every job, and
any job's own in its place.
"""
from __future__ import annotations

import datetime
from typing import Any, Dict, List, Mapping, Optional

from sqlalchemy.orm import Session

from ..project import Project
from .db import Setting

STALENESS_SETTING = 'staleness'
# A nightly job that misses one night is stale by the next morning.
DEFAULT_STALE_HOURS = 26
# How far back each job's history is read for its last completed run.
HISTORY_PER_JOB = 200


def _iso(seconds: Optional[float]) -> Optional[str]:

    if seconds is None:
        return None

    return datetime.datetime.fromtimestamp(seconds, datetime.timezone.utc).isoformat(timespec='seconds')


def staleness(session: Session) -> Dict[str, Any]:
    """The thresholds: `defaultHours`, and `jobs`, each job's own."""

    found = session.get(Setting, STALENESS_SETTING)
    value = dict(found.value) if found is not None and isinstance(found.value, dict) else {}

    return {'defaultHours': float(value.get('defaultHours', DEFAULT_STALE_HOURS)), 'jobs': dict(value.get('jobs', {}))}


def setStaleness(session: Session, defaultHours: float, jobs: Mapping[str, float]) -> Dict[str, Any]:

    if defaultHours <= 0 or any(hours <= 0 for hours in jobs.values()):
        raise ValueError('hours must be more than 0')
    value = {'defaultHours': defaultHours, 'jobs': dict(jobs)}
    found = session.get(Setting, STALENESS_SETTING)
    if found is None:
        session.add(Setting(name=STALENESS_SETTING, value=value))
    else:
        found.value = value

    return value


def lastCompleted(project: Project) -> Dict[str, Optional[str]]:
    """When each job last completed, as history records it, or None."""

    history = project.history()
    found: Dict[str, Optional[str]] = {}
    for name in project.jobsFile.jobs:
        records = history.read(limit=HISTORY_PER_JOB, job=name) if history is not None else []
        found[name] = next((record['finishedAt'] for record in records if record['status'] == 'completed'), None)

    return found


def jobSchedule(project: Project, thresholds: Mapping[str, Any], now: Optional[datetime.datetime] = None) -> List[Dict[str, Any]]:
    """Each job's place in the schedule, as `bauta jobs` reckons it, with when
    it last completed and whether that makes it stale. The watermark `bauta
    jobs` shows is left out: it is a value from a table.
    """

    now = now or datetime.datetime.now(datetime.timezone.utc)
    completed = lastCompleted(project)
    entries = []
    for entry in project.schedule():
        name = entry['job']
        hours = float(thresholds['jobs'].get(name, thresholds['defaultHours']))
        last = completed.get(name)
        active = entry['state'] != 'inactive'
        if not active:
            stale = False
        elif last is None:
            stale = True
        else:
            stale = (now - datetime.datetime.fromisoformat(last)).total_seconds() > hours * 3600
        entries.append({'job': name, 'state': entry['state'], 'waitsFor': entry['waitsFor'], 'refreshMinutes': entry['refreshMinutes'],
                        'lastRunAt': _iso(entry['lastRunAt']), 'nextDueAt': _iso(entry['nextDueAt']), 'lastCompletedAt': last,
                        'staleAfterHours': hours, 'ownThreshold': name in thresholds['jobs'], 'stale': stale, 'neverCompleted': active and last is None})

    return entries
