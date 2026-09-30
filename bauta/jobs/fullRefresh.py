"""`run --full-refresh`: an incremental job's whole source, loaded as a swap
or an overwrite, so rows deleted from the source leave the copy. Watermarks
can't see a hard delete; replacing the copy on a schedule bounds how long a
deleted row outlives its source.

The job's own query needs no rewriting: bound to `watermarkInitial`, the
value its first run binds, it returns every row. What the refresh read up
to is then recorded as the watermark, so the next incremental run carries
on from there.
"""
from __future__ import annotations

from typing import Dict, List, Mapping, Optional

from ..configuration import (ConfigurationError, ConnectionConfig, DataJobConfig, FilesConnection, IcebergConnection, InsertStrategy)


def isFullRefresh(job: DataJobConfig) -> bool:
    """Whether `job` is one fullRefreshJobs made: a watermark with a strategy
    that replaces the target. Validation refuses that in any configuration
    written by hand, since it would replace the target with only the rows
    that changed; a full refresh binds watermarkInitial instead, so it
    replaces it with every row.
    """

    return job.watermarkColumn is not None and job.insertStrategy in (InsertStrategy.SWAP, InsertStrategy.OVERWRITE)


def fullRefreshProblem(job: DataJobConfig, connection: Optional[ConnectionConfig]) -> Optional[str]:
    """Why --full-refresh can't replace incremental `job`'s target, or None:
    a database target with no targetTableStage to load into before the swap,
    or files, whose appended parts an overwrite's snapshot directories would
    replace with a different layout under their readers. `audit` asks this
    too, so the gap shows before the scheduled refresh fails.
    """

    if isinstance(connection, FilesConnection):
        return ('appends to files, and a full refresh would publish snapshot directories in place of the parts its readers expect. '
                'Give it an overwrite job of its own to refresh it')
    if not isinstance(connection, IcebergConnection) and not job.targetTableStage:
        return ('has no targetTableStage, which a full refresh loads before swapping it with {}. Set it, and create the stage table with '
                '`bauta schema --stage-suffix`'.format(job.targetTableFinal))

    return None


def fullRefreshJobs(jobs: Mapping[str, DataJobConfig], connections: Mapping[str, ConnectionConfig]) -> Dict[str, DataJobConfig]:
    """`jobs` with each incremental one made to load its whole source and
    replace its target: through its stage table and a swap on a database, in
    one overwrite commit on Iceberg. Other jobs already load everything each
    run, and are returned as they are.

    Refused, naming every job at once, where fullRefreshProblem says a job
    can't be replaced safely.
    """

    refreshed: Dict[str, DataJobConfig] = {}
    problems: List[str] = []

    for name, job in jobs.items():
        if not job.watermarkColumn or not job.active:
            refreshed[name] = job
            continue

        connection = connections.get(job.targetConnection)
        problem = fullRefreshProblem(job, connection)
        if problem:
            problems.append('{} {}'.format(name, problem))
        elif isinstance(connection, IcebergConnection):
            refreshed[name] = job.model_copy(update={'insertStrategy': InsertStrategy.OVERWRITE, 'refresh': None})
        else:
            refreshed[name] = job.model_copy(update={'insertStrategy': InsertStrategy.SWAP, 'refresh': None})

    if problems:
        raise ConfigurationError('--full-refresh cannot replace every incremental job: ' + '; '.join(problems))

    return refreshed
