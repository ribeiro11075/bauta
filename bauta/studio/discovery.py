"""Discovery review: what `discover --update` finds -- columns a source's
query now returns that a job's policy doesn't name, with the policy discover
proposes for each, and named columns it no longer returns -- kept for review,
and applied to the jobs files only when an admin accepts them, through the
same checks as any other edit.
"""
from __future__ import annotations

import os
from typing import Any, Dict, List, Sequence

from ..project import Project
from .configfiles import EditRefused, digest, writeFile
from .settings import Settings


def findings(project: Project, sample: int = 1000) -> Dict[str, Any]:
    """Each drifted job, as kept for review: its file, the columns to add with
    their proposed policy and the reason, and those to remove. No value read
    leaves this: a reason names what a column looks like, never what it holds.
    """

    drifted = project.drift(sample=sample)
    jobs = []
    for drift in drifted:
        origin = project.document.origins.get(drift.job)
        jobs.append({'job': drift.job, 'file': os.path.relpath(origin, project.directory) if origin is not None else None,
                     'added': [{'column': suggestion.column, 'policy': suggestion.policy, 'reason': suggestion.reason} for suggestion in drift.added],
                     'removed': list(drift.removed)})

    masked = sum(1 for job in project.jobsFile.jobs.values() if job.masking is not None)

    return {'jobs': jobs, 'checkedJobs': masked}


def apply(settings: Settings, project: Project, kept: Dict[str, Any], accepted: Sequence[str]) -> List[Dict[str, Any]]:
    """Writes the kept proposals for the `accepted` jobs into the files they
    are defined in, one file at a time, each validated, backed up and diffed
    as any edit is. Returns each file's write.
    """

    from ..generate.discovery import Suggestion
    from ..generate.drift import DriftError, JobDrift, applyDrift

    byJob = {entry['job']: entry for entry in kept.get('jobs', [])}
    unknown = [job for job in accepted if job not in byJob]
    if unknown:
        raise EditRefused('no proposal kept for {}; check discovery again'.format(', '.join(unknown)))

    byFile: Dict[str, List[Any]] = {}
    for job in accepted:
        entry = byJob[job]
        if entry['file'] is None:
            raise EditRefused('{} is not defined in any jobs file'.format(job))
        drift = JobDrift(job=job, added=[Suggestion(column=added['column'], policy=added['policy'], reason=added['reason']) for added in entry['added']],
                         removed=list(entry['removed']))
        byFile.setdefault(entry['file'], []).append(drift)

    written = []
    for relative, drifts in byFile.items():
        path = project.directory / relative
        before = path.read_text()
        text = before
        try:
            for drift in drifts:
                text = applyDrift(text, drift)
        except DriftError as error:
            raise EditRefused('{}: {}. Apply it by hand in the editor.'.format(relative, error)) from error
        written.append(writeFile(settings, project, relative, text, digest(before)))

    return written
