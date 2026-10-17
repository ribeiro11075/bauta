"""What the reviews learn by connecting: each job's columns and a sample of
its rows, its target's columns, whether each connection is encrypted, and the
foreign keys that apply to each copy. Nothing here writes, and no value read
is ever reported.
"""
from __future__ import annotations

import logging
from typing import Any, Dict, List, Mapping, Optional, Sequence, Set, Tuple

from ..configuration import EMBEDDED_TYPES, ConnectionConfig, DataJobConfig, isLake
from ..database import Database
from ..database.dialects import ForeignKey, splitTableName, unqualifiedName
from ..database.sessions import Connections, sourceQuerySample, targetColumns
from ..log import LOGGER_NAME
from ..log.scrubbing import describeError
from .audit import ConnectedFacts

logger = logging.getLogger(LOGGER_NAME)


def schemasUsed(jobs: Mapping[str, Any], alias: str, sources: bool = True, targets: bool = True) -> List[Optional[str]]:
    """The schemas `alias`'s jobs read from or load into, None being the
    connection's own: a target's from targetTableFinal, a source's from a
    sourceQuery that reads one table whole. Any other query could read any
    schema, so it counts as the connection's own, the one its bare names mean.
    """

    from ..generate.drift import _wholeTable

    schemas: Set[Optional[str]] = set()
    for job in jobs.values():
        if targets and job.targetConnection == alias:
            schemas.add(splitTableName(job.targetTableFinal)[0])
        if sources and job.sourceConnection == alias:
            table = _wholeTable(job.sourceQuery)
            schemas.add(splitTableName(table)[0] if table else None)

    return sorted(schemas, key=lambda schema: schema or '') or [None]


def readForeignKeys(database: Database, schemas: Sequence[Optional[str]]) -> Tuple[List[ForeignKey], Dict[str, int]]:
    """The keys declared in `schemas`, named without their schema, as a copy
    in another schema is matched to them; and, when there are none, the other
    schemas that do declare some.
    """

    keys = [foreignKey._replace(table=unqualifiedName(foreignKey.table), referencedTable=unqualifiedName(foreignKey.referencedTable))
            for schema in schemas for foreignKey in database.getForeignKeys(schema)]

    return keys, ({} if keys else database.foreignKeysElsewhere(schemas))


def connectedFacts(jobs: Mapping[str, DataJobConfig], connectionConfiguration: Mapping[str, ConnectionConfig], sample: int) -> ConnectedFacts:
    """What `audit --connect` learns by connecting: each job's columns and up
    to `sample` of its rows, its target's columns, whether each connection is
    encrypted, and the foreign keys that apply to each target. What can't be
    read is recorded as such, for the report to say, rather than stopping it.
    """

    returnedColumns: Dict[str, List[str]] = {}
    targetColumnsByJob: Dict[str, List[str]] = {}
    unreachable: Dict[str, str] = {}
    unreadableTargets: Dict[str, str] = {}
    encryption: Dict[str, Optional[bool]] = {}
    foreignKeys: Dict[str, List[ForeignKey]] = {}
    declaredForeignKeys: Dict[str, List[ForeignKey]] = {}
    elsewhere: Dict[str, Dict[str, int]] = {}
    samples: Dict[str, List[Tuple[Any, ...]]] = {}
    # Every job's columns, not only a masked one's: an unmasked job's are
    # what says whether it is carrying personal data (see audit._auditUnmaskedJob).
    keysByAlias: Dict[str, List[ForeignKey]] = {}

    with Connections(connectionConfiguration) as connections:
        for name, job in jobs.items():
            try:
                returnedColumns[name], samples[name] = sourceQuerySample(job, connections, sample)
            except Exception as error:
                unreachable[name] = describeError(error)
                continue
            if job.masking is not None:
                # A file or Iceberg target writes the columns the query returns.
                writesLake = isLake(connectionConfiguration[job.targetConnection])
                try:
                    targetColumnsByJob[name] = job.targetColumns or (returnedColumns[name] if writesLake else targetColumns(job, connections))
                except Exception as error:
                    # Its own finding: the query ran, and saying it
                    # didn't sends a reader to the wrong database.
                    unreadableTargets[name] = describeError(error)

        for alias in sorted({job.sourceConnection for job in jobs.values()} | {job.targetConnection for job in jobs.values()}):
            if isLake(connectionConfiguration[alias]):
                # Files or Iceberg: no connection to ask of encryption,
                # and no foreign keys.
                continue
            isLocal = connectionConfiguration[alias].type in EMBEDDED_TYPES
            try:
                with connections.use(alias) as database:
                    if not isLocal:
                        encryption[alias] = database.isEncrypted()
            except Exception as error:
                logger.warning('{}: could not connect to {} to check encryption and foreign keys -- {}'.format(
                    alias, connectionConfiguration[alias].describeTarget(), describeError(error)))
                if not isLocal:
                    encryption[alias] = None
                continue
            try:
                with connections.use(alias) as database:
                    keysByAlias[alias], elsewhere[alias] = readForeignKeys(database, schemasUsed(jobs, alias))
            except Exception as error:
                logger.warning('{}: could not read foreign keys -- {}'.format(alias, describeError(error)))

    # The keys that apply to a copy are the target's own and those of the
    # sources it is copied from, which a target often doesn't declare.
    # Both are matched to jobs by table name.
    for target in {job.targetConnection for job in jobs.values()}:
        sources = {job.sourceConnection for job in jobs.values() if job.targetConnection == target}
        unique: Dict[Any, ForeignKey] = {}
        for alias in [target] + sorted(sources):
            for foreignKey in keysByAlias.get(alias, []):
                folded = (foreignKey.table.upper(), tuple(column.upper() for column in foreignKey.columns),
                          foreignKey.referencedTable.upper(), tuple(column.upper() for column in foreignKey.referencedColumns))
                unique.setdefault(folded, foreignKey)
        foreignKeys[target] = list(unique.values())
        # Only a target whose keys could be read, or every source key would
        # read as one it doesn't declare.
        if target in keysByAlias:
            declaredForeignKeys[target] = keysByAlias[target]

    return ConnectedFacts(returnedColumns=returnedColumns, samples=samples, unreachable=unreachable, unreadableTargets=unreadableTargets,
                          targetColumns=targetColumnsByJob, encryption=encryption, foreignKeys=foreignKeys, declaredForeignKeys=declaredForeignKeys,
                          foreignKeysElsewhere=elsewhere)
