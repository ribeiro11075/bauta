"""The checks made before a run starts that a masked upsert job's key hasn't
changed under it, which would leave two keys' masks in one target.
"""
from __future__ import annotations

import logging
from typing import List, Mapping, Optional, Sequence, Tuple

from ..configuration import ConfigurationError, DatabaseConnectionConfig, DataJobConfig, DataJobsFile, InsertStrategy
from ..database import Database
from ..log import LOGGER_NAME
from ..masking import changesValues, keyFingerprint, maskingImplementation, policyFor, splitMaskingIdentity
from .memory import MemoryBackend

logger = logging.getLogger(LOGGER_NAME)


def _maskedPrimaryKeyColumns(jobConfig: DataJobConfig, primaryKeyColumns: Sequence[str]) -> List[str]:
    """The target's primary key columns this job's policy changes.

    An upsert matches rows on the primary key, so masking one means a new key
    produces new keys: the rows are inserted beside the old ones rather than
    updating them. Names are matched case-insensitively, as a policy is bound.
    """

    masking = jobConfig.masking
    if masking is None:
        return []

    return [column for column in primaryKeyColumns
            if (policy := policyFor(column, masking.columns, masking.defaultStrategy)) is not None and changesValues(policy)]


def _refuseAcceptedKeyChangeThatWouldDuplicateRows(changed: Sequence[Tuple[str, DataJobConfig]],
                                                   databaseConfiguration: Mapping[str, DatabaseConnectionConfig]) -> None:
    """--accept-key-change is safe only where the masked rows still match the
    ones already loaded. Where the policy masks the target's primary key, they
    cannot: the run inserts a second generation of rows beside the first, and
    where a new key collides with an old one it overwrites a different row.

    Only the jobs whose key actually changed are checked, so a run with no
    change connects to nothing.
    """

    refused = []

    for name, jobConfig in changed:
        settings = databaseConfiguration.get(jobConfig.targetDatabase)
        if settings is None:
            continue
        try:
            with Database(connectionSettings=settings) as database:
                primaryKeyColumns = database.getPrimaryColumnNames(table=jobConfig.targetTableFinal)
        except Exception as error:
            logger.warning('{}: could not check whether the masking key change is safe to accept -- {}'.format(name, error))
            continue

        masked = _maskedPrimaryKeyColumns(jobConfig, primaryKeyColumns)
        if masked:
            refused.append('{} (masks {} of {})'.format(name, ', '.join(masked), jobConfig.targetTableFinal))

    if refused:
        raise ConfigurationError(
            'the masking key changed for upsert job(s) {}, whose policy masks the primary key of the table they load. A new key '
            'gives those rows new primary keys, so the run would insert a second generation beside the first rather than update '
            'it -- and where a new key lands on an old one, overwrite a different row. --accept-key-change cannot make that safe. '
            'Empty those targets with `bauta clear`, which also forgets the old key, and run again'.format(', '.join(refused)))


def _requireUnchangedMaskingKeys(jobsFile: DataJobsFile, memory: MemoryBackend, acceptKeyChange: bool,
                                 databaseConfiguration: Optional[Mapping[str, DatabaseConnectionConfig]] = None) -> None:
    """Refuses to run an upsert job whose masking key changed since it last
    completed: its target's existing rows would no longer join with new ones.
    A swap job replaces its whole target, so it isn't checked.

    `--accept-key-change` acknowledges that for jobs where re-loading under a
    new key merely rewrites the rows. Where the policy masks the target's
    primary key it does not, and the change is refused whatever the flag says;
    see _refuseAcceptedKeyChangeThatWouldDuplicateRows.
    """

    recorded = memory.readKeyFingerprints()
    changed = []
    changedJobs: List[Tuple[str, DataJobConfig]] = []
    reimplemented = []

    for name, job in sorted(jobsFile.jobs.items()):
        if not job.active or job.masking is None or job.insertStrategy != InsertStrategy.UPSERT:
            continue

        if recorded.get(name) is None:
            continue

        previousKey, previousImplementation = splitMaskingIdentity(recorded[name])
        currentKey = keyFingerprint(job.masking.key.get_secret_value())

        if previousKey != currentKey:
            changed.append('{} (was {}, now {})'.format(name, previousKey, currentKey))
            changedJobs.append((name, job))
        elif previousImplementation != maskingImplementation():
            reimplemented.append('{} (was {}, now {})'.format(name, previousImplementation, maskingImplementation()))

    # Warned rather than refused: the implementations are tested to agree (see
    # maskingIdentity), and refusing would stop every upsert job whenever the
    # extension was installed.
    if reimplemented:
        logger.warning('Masking implementation changed since the last run of upsert job(s) {}. The two are tested to produce '
                       'identical masks, so this is recorded rather than refused -- but if rows masked before and after stop '
                       'joining, this is why'.format(', '.join(reimplemented)))

    if not changed:
        return

    if acceptKeyChange:
        if databaseConfiguration is not None:
            _refuseAcceptedKeyChangeThatWouldDuplicateRows(changedJobs, databaseConfiguration)
        logger.warning('Masking key changed for {}; continuing, as acknowledged'.format(', '.join(changed)))
        return

    raise ConfigurationError(
        'the masking key changed since the last run of upsert job(s) {}. Their targets still hold rows masked under the old key, '
        'which would no longer match rows masked under the new one. Empty those targets with `bauta clear`, which also forgets '
        'the old key, and run again. Where re-loading under the new key merely rewrites the rows -- the target\'s primary key is '
        'not masked -- --accept-key-change runs them as they are instead'.format(', '.join(changed)))
