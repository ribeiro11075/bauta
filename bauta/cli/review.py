"""The commands that report without moving data: audit, coverage and
verify-references.
"""
from __future__ import annotations

import argparse
import json
from typing import Any, Dict, List, Mapping, Optional, Sequence, Set, Tuple

from ..configuration import EMBEDDED_TYPES, isLake
from ..database import Database
from ..database.dialects import ForeignKey, bareName, splitTableName, unqualifiedName
from ..log import Log
from ..log.scrubbing import describeError
from .common import (EXIT_JOBS_DID_NOT_SUCCEED, EXIT_SUCCESS, _Connections, UsageError, _discoveryRules, _loadDataJobs, _requireDatabase, _selectJobs,
                     _sourceQuerySample, _targetColumns, _toolVersion, _writeOutput)


def _commandVerifyReferences(arguments: argparse.Namespace, log: Log) -> int:
    """Counts the rows in each target whose foreign key points at nothing: the
    keys the target declares, and those of the sources copied into it, which a
    copy often lacks. Reads the sources' catalogs only, never their rows.

    Exits 1 if any key has orphaned rows or couldn't be checked.
    """

    import datetime

    from ..review.references import referencesReport, renderReferences, summarize, uncheckedNote, verifyReferences

    jobsFile, connectionConfiguration = _loadDataJobs(arguments)
    jobs = {name: job for name, job in _selectJobs(jobsFile.jobs, arguments.job, log).items() if arguments.job or job.active}
    keysByAlias: Dict[str, List[ForeignKey]] = {}
    elsewhere: Dict[str, Dict[str, int]] = {}

    for alias in sorted({job.sourceConnection for job in jobs.values()}):
        try:
            with Database(connectionSettings=connectionConfiguration[alias]) as database:
                keysByAlias[alias], elsewhere[alias] = _readForeignKeys(database, _schemasUsed(jobs, alias, targets=False))
        except Exception as error:
            log.logging.warning('{}: could not read foreign keys, so only the target\'s own are checked -- {}'.format(alias, describeError(error)))

    results = []
    notes = []
    for target in sorted({job.targetConnection for job in jobs.values() if not isLake(connectionConfiguration[job.targetConnection])}):
        targetJobs = [job for job in jobs.values() if job.targetConnection == target]
        # Keyed bare, as the catalogs report names: a job naming a reserved
        # word writes it quoted, and a quoted key would match nothing.
        loaded = {bareName(unqualifiedName(job.targetTableFinal)).upper(): job.targetTableFinal for job in targetJobs}
        sources = sorted({job.sourceConnection for job in targetJobs} - {target})
        sourceKeys = [foreignKey for alias in sources for foreignKey in keysByAlias.get(alias, [])]
        with Database(connectionSettings=connectionConfiguration[target]) as database:
            checked = verifyReferences(database, target, loaded, sourceKeys)
            if not checked:
                # Nothing to count is a pass only where there is nothing to
                # find: keys declared in a schema these jobs don't use mean
                # the check looked in the wrong place.
                missed = {target: database.foreignKeysElsewhere(_schemasUsed(jobs, target, sources=False))}
                missed.update({alias: elsewhere.get(alias, {}) for alias in sources})
                notes.append(uncheckedNote(target, {alias: counts for alias, counts in missed.items() if counts}))
        results.extend(checked)

    generatedAt = datetime.datetime.now(datetime.timezone.utc).isoformat(timespec='seconds')
    text = (json.dumps(referencesReport(results, generatedAt, notes), indent=2, default=str) + '\n' if arguments.format == 'json'
            else renderReferences(results, notes))
    _writeOutput(text, arguments.output)
    log.logging.info(summarize(results))

    if any(result.orphans != 0 for result in results) or any(note.missed for note in notes):
        return EXIT_JOBS_DID_NOT_SUCCEED

    return EXIT_SUCCESS


def _schemasUsed(jobs: Mapping[str, Any], alias: str, sources: bool = True, targets: bool = True) -> List[Optional[str]]:
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


def _readForeignKeys(database: Database, schemas: Sequence[Optional[str]]) -> Tuple[List[ForeignKey], Dict[str, int]]:
    """The keys declared in `schemas`, named without their schema, as a copy
    in another schema is matched to them; and, when there are none, the other
    schemas that do declare some.
    """

    keys = [foreignKey._replace(table=unqualifiedName(foreignKey.table), referencedTable=unqualifiedName(foreignKey.referencedTable))
            for schema in schemas for foreignKey in database.getForeignKeys(schema)]

    return keys, ({} if keys else database.foreignKeysElsewhere(schemas))


def _commandCoverage(arguments: argparse.Namespace, log: Log) -> int:
    """Lists every table in a source database and what the jobs do with it.

    `audit` checks the jobs that exist; this one finds what no job covers at
    all, which nothing else can see -- a table with no job has no audit.

    Exits 1 when any table is neither copied nor declared in `acknowledged`.
    """

    from ..review.coverage import UNCOVERED, coverageReport, jobsReading, renderCoverage

    jobsFile, connectionConfiguration = _loadDataJobs(arguments)
    jobs = {name: job for name, job in _selectJobs(jobsFile.jobs, arguments.job, log).items() if arguments.job or job.active}
    alias = arguments.connection or _theOnlySourceConnection(jobs)
    _requireDatabase(connectionConfiguration, alias)

    with Database(connectionSettings=connectionConfiguration[alias]) as database:
        tables = database.listTables(schema=arguments.schema)
        # Only for the tables nothing covers: the rest are already accounted
        # for, and reading every column of a whole schema is not free.
        covered = {table for table in tables if jobsReading(table, alias, jobs)}
        columns = {}
        for table in tables:
            if table in covered:
                continue
            try:
                columns[table] = database.getAllColumnNames(table=table)
            except Exception as error:
                log.logging.warning('{}: could not read its columns -- {}'.format(table, describeError(error)))

    report = coverageReport(alias, tables, jobs, acknowledged=jobsFile.acknowledged.get(alias),
                            columns=columns, rules=_discoveryRules(arguments))
    if arguments.format == 'html':
        import datetime

        from ..review.html import renderCoverageHtml

        generatedAt = datetime.datetime.now(datetime.timezone.utc).isoformat(timespec='seconds')
        text = renderCoverageHtml(report, generatedAt=generatedAt, version=_toolVersion())
    else:
        text = json.dumps(report, indent=2, default=str) + '\n' if arguments.format == 'json' else renderCoverage(report)
    _writeOutput(text, arguments.output)

    if report['summary'][UNCOVERED]:
        return EXIT_JOBS_DID_NOT_SUCCEED

    return EXIT_SUCCESS


def _theOnlySourceConnection(jobs: Mapping[str, Any]) -> str:
    """The alias every job reads from, when --connection isn't given."""

    aliases = sorted({job.sourceConnection for job in jobs.values()})

    if len(aliases) != 1:
        raise UsageError('--connection is required: the jobs read from {}'.format(
            ', '.join(aliases) if aliases else 'no database'))

    return aliases[0]


def _commandAudit(arguments: argparse.Namespace, log: Log) -> int:
    """Reports what each job does with data, and anything a reviewer should
    question. Offline unless --connect, which also resolves each masked
    query's real columns and checks whether each connection is encrypted.

    With --connect it also reads up to --sample rows of each query, to
    question a column kept as it is whose values look like personal data, and
    columns sharing a domain that hold its values differently. The rows stay
    in memory, and no value is reported.

    Exits 1 on an error finding, and with --strict on a warning too.
    """

    from ..review.audit import auditJobs, renderAudit

    jobsFile, connectionConfiguration = _loadDataJobs(arguments)
    jobs = _selectJobs(jobsFile.jobs, arguments.job, log)
    returnedColumns: Dict[str, List[str]] = {}
    targetColumns: Dict[str, List[str]] = {}
    unreachable: Dict[str, str] = {}
    unreadableTargets: Dict[str, str] = {}
    encryption: Dict[str, Optional[bool]] = {}
    foreignKeys: Dict[str, List[ForeignKey]] = {}
    declaredForeignKeys: Dict[str, List[ForeignKey]] = {}
    elsewhere: Dict[str, Dict[str, int]] = {}
    samples: Dict[str, List[Tuple[Any, ...]]] = {}

    if arguments.connect:
        # Every job's columns, not only a masked one's: an unmasked job's are
        # what says whether it is carrying personal data (see audit._auditUnmaskedJob).
        keysByAlias: Dict[str, List[ForeignKey]] = {}

        with _Connections(connectionConfiguration) as connections:
            for name, job in jobs.items():
                try:
                    returnedColumns[name], samples[name] = _sourceQuerySample(job, connections, arguments.sample)
                except Exception as error:
                    unreachable[name] = describeError(error)
                    continue
                if job.masking is not None:
                    # A file or Iceberg target writes the columns the query returns.
                    writesLake = isLake(connectionConfiguration[job.targetConnection])
                    try:
                        targetColumns[name] = job.targetColumns or (returnedColumns[name] if writesLake else _targetColumns(job, connections))
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
                    log.logging.warning('{}: could not connect to {} to check encryption and foreign keys -- {}'.format(
                        alias, connectionConfiguration[alias].describeTarget(), describeError(error)))
                    if not isLocal:
                        encryption[alias] = None
                    continue
                try:
                    with connections.use(alias) as database:
                        keysByAlias[alias], elsewhere[alias] = _readForeignKeys(database, _schemasUsed(jobs, alias))
                except Exception as error:
                    log.logging.warning('{}: could not read foreign keys -- {}'.format(alias, describeError(error)))

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

    report = auditJobs(jobs, returnedColumns=returnedColumns, encryption=encryption, unreachable=unreachable,
                       targetColumns=targetColumns, foreignKeys=foreignKeys, declaredForeignKeys=declaredForeignKeys,
                       rules=_discoveryRules(arguments), connections=connectionConfiguration,
                       foreignKeysElsewhere=elsewhere if arguments.connect else None, unreadableTargets=unreadableTargets,
                       samples=samples)
    if arguments.format == 'html':
        from ..review.html import renderAuditHtml

        text = renderAuditHtml(report, version=_toolVersion())
    else:
        text = json.dumps(report, indent=2, default=str) + '\n' if arguments.format == 'json' else renderAudit(report)
    _writeOutput(text, arguments.output)

    if report['summary']['error'] or (arguments.strict and report['summary']['warning']):
        return EXIT_JOBS_DID_NOT_SUCCEED

    return EXIT_SUCCESS
