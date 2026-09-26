"""The commands that report without moving data: audit, coverage and
verify-references.
"""
from __future__ import annotations

import argparse
import json
from typing import Any, Dict, List, Mapping, Optional

from ..configuration import EMBEDDED_TYPES
from ..database import Database
from ..database.dialects import ForeignKey, bareName, unqualifiedName
from ..log import Log
from ..log.scrubbing import describeError
from .common import (EXIT_JOBS_DID_NOT_SUCCEED, EXIT_SUCCESS, _Connections, UsageError, _discoveryRules, _loadDataJobs, _requireAlias, _selectJobs,
                     _sourceQueryColumns, _targetColumns, _writeOutput)


def _commandVerifyReferences(arguments: argparse.Namespace, log: Log) -> int:
    """Counts the rows in each target whose foreign key points at nothing: the
    keys the target declares, and those of the sources copied into it, which a
    copy often lacks. Reads the sources' catalogs only, never their rows.

    Exits 1 if any key has orphaned rows or couldn't be checked.
    """

    import datetime

    from ..review.references import referencesReport, renderReferences, summarize, verifyReferences

    jobsFile, connectionConfiguration = _loadDataJobs(arguments)
    jobs = {name: job for name, job in _selectJobs(jobsFile.jobs, arguments.job, log).items() if arguments.job or job.active}
    keysByAlias: Dict[str, List[ForeignKey]] = {}

    for alias in sorted({job.sourceConnection for job in jobs.values()}):
        try:
            with Database(connectionSettings=connectionConfiguration[alias]) as database:
                keysByAlias[alias] = database.getForeignKeys()
        except Exception as error:
            log.logging.warning('{}: could not read foreign keys, so only the target\'s own are checked -- {}'.format(alias, describeError(error)))

    results = []
    for target in sorted({job.targetConnection for job in jobs.values()}):
        targetJobs = [job for job in jobs.values() if job.targetConnection == target]
        # Keyed bare, as the catalogs report names: a job naming a reserved
        # word writes it quoted, and a quoted key would match nothing.
        loaded = {bareName(unqualifiedName(job.targetTableFinal)).upper(): job.targetTableFinal for job in targetJobs}
        sourceKeys = [foreignKey for alias in sorted({job.sourceConnection for job in targetJobs} - {target})
                      for foreignKey in keysByAlias.get(alias, [])]
        with Database(connectionSettings=connectionConfiguration[target]) as database:
            results.extend(verifyReferences(database, target, loaded, sourceKeys))

    generatedAt = datetime.datetime.now(datetime.timezone.utc).isoformat(timespec='seconds')
    text = json.dumps(referencesReport(results, generatedAt), indent=2, default=str) + '\n' if arguments.format == 'json' else renderReferences(results)
    _writeOutput(text, arguments.output)
    log.logging.info(summarize(results))

    if any(result.orphans != 0 for result in results):
        return EXIT_JOBS_DID_NOT_SUCCEED

    return EXIT_SUCCESS


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
    _requireAlias(connectionConfiguration, alias)

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
    _writeOutput(json.dumps(report, indent=2, default=str) + '\n' if arguments.format == 'json' else renderCoverage(report),
                 arguments.output)

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

    Exits 1 on an error finding, and with --strict on a warning too.
    """

    from ..review.audit import auditJobs, renderAudit

    jobsFile, connectionConfiguration = _loadDataJobs(arguments)
    jobs = _selectJobs(jobsFile.jobs, arguments.job, log)
    returnedColumns: Dict[str, List[str]] = {}
    targetColumns: Dict[str, List[str]] = {}
    unreachable: Dict[str, str] = {}
    encryption: Dict[str, Optional[bool]] = {}
    foreignKeys: Dict[str, List[ForeignKey]] = {}
    declaredForeignKeys: Dict[str, List[ForeignKey]] = {}

    if arguments.connect:
        # Every job's columns, not only a masked one's: an unmasked job's are
        # what says whether it is carrying personal data (see audit._auditUnmaskedJob).
        keysByAlias: Dict[str, List[ForeignKey]] = {}

        with _Connections(connectionConfiguration) as connections:
            for name, job in jobs.items():
                try:
                    returnedColumns[name] = _sourceQueryColumns(job, connections)
                    if job.masking is not None:
                        targetColumns[name] = job.targetColumns or _targetColumns(job, connections)
                except Exception as error:
                    unreachable[name] = describeError(error)

            for alias in sorted({job.sourceConnection for job in jobs.values()} | {job.targetConnection for job in jobs.values()}):
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
                        keysByAlias[alias] = database.getForeignKeys()
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
            declaredForeignKeys[target] = keysByAlias.get(target, [])

    report = auditJobs(jobs, returnedColumns=returnedColumns, encryption=encryption, unreachable=unreachable,
                       targetColumns=targetColumns, foreignKeys=foreignKeys, declaredForeignKeys=declaredForeignKeys,
                       rules=_discoveryRules(arguments))
    _writeOutput(json.dumps(report, indent=2, default=str) + '\n' if arguments.format == 'json' else renderAudit(report), arguments.output)

    if report['summary']['error'] or (arguments.strict and report['summary']['warning']):
        return EXIT_JOBS_DID_NOT_SUCCEED

    return EXIT_SUCCESS
