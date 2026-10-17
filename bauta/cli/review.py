"""The commands that report without moving data: audit, coverage,
verify-references and bench.
"""
from __future__ import annotations

import argparse
import json

from ..log import Log
from .common import (EXIT_JOBS_DID_NOT_SUCCEED, EXIT_SUCCESS, UsageError, _discoveryRules, _loadDataJobs, _loadProject, _selectJobs, _toolVersion,
                     _writeOutput)


def _commandVerifyReferences(arguments: argparse.Namespace, log: Log) -> int:
    """Counts the rows in each target whose foreign key points at nothing: the
    keys the target declares, and those of the sources copied into it, which a
    copy often lacks. Reads the sources' catalogs only, never their rows.

    Exits 1 if any key has orphaned rows or couldn't be checked.
    """

    import datetime

    from ..review.references import referencesReport, renderReferences, summarize

    project = _loadProject(arguments)
    _selectJobs(project.jobsFile.jobs, arguments.job, log)
    results, notes = project.verifyReferences(jobs=arguments.job)

    generatedAt = datetime.datetime.now(datetime.timezone.utc).isoformat(timespec='seconds')
    text = (json.dumps(referencesReport(results, generatedAt, notes), indent=2, default=str) + '\n' if arguments.format == 'json'
            else renderReferences(results, notes))
    _writeOutput(text, arguments.output)
    log.logging.info(summarize(results))

    if any(result.orphans != 0 for result in results) or any(note.missed for note in notes):
        return EXIT_JOBS_DID_NOT_SUCCEED

    return EXIT_SUCCESS


def _commandCoverage(arguments: argparse.Namespace, log: Log) -> int:
    """Lists every table in a source database and what the jobs do with it.

    `audit` checks the jobs that exist; this one finds what no job covers at
    all, which nothing else can see -- a table with no job has no audit.

    Exits 1 when any table is neither copied nor declared in `acknowledged`.
    """

    from ..review.coverage import UNCOVERED, renderCoverage

    report = _loadProject(arguments).coverage(connection=arguments.connection, schema=arguments.schema, jobs=arguments.job,
                                              rules=_discoveryRules(arguments))
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

    from ..review.audit import renderAudit

    project = _loadProject(arguments)
    _selectJobs(project.jobsFile.jobs, arguments.job, log)

    if not arguments.connect:
        # Offline, a column is judged by the name its policy gives it, so
        # `email AS contact` kept passes as a contact; its sampled values
        # would have said otherwise.
        log.logging.info('Auditing the configuration alone: a column is judged by its name, so one renamed in sourceQuery -- email AS '
                         'contact -- passes. Gate CI with audit --connect --strict, which samples what each query returns')

    report = project.audit(jobs=arguments.job, connect=arguments.connect, sample=arguments.sample, rules=_discoveryRules(arguments))
    if arguments.format == 'html':
        from ..review.html import renderAuditHtml

        text = renderAuditHtml(report, version=_toolVersion())
    else:
        text = json.dumps(report, indent=2, default=str) + '\n' if arguments.format == 'json' else renderAudit(report)
    _writeOutput(text, arguments.output)

    if report['summary']['error'] or (arguments.strict and report['summary']['warning']):
        return EXIT_JOBS_DID_NOT_SUCCEED

    return EXIT_SUCCESS


def _commandBench(arguments: argparse.Namespace, log: Log) -> int:
    """Reads up to --rows of each job's query from its real source,
    transforming and masking as `run` would and writing nothing, and reports
    how fast each stage went. Exits 1 if any job couldn't be measured.
    """

    from ..configuration import ConfigurationError
    from ..masking import effectiveMaskingThreads, maskingThreadsFor, setMaskingThreads
    from ..review.bench import benchJob, benchReport, masker, renderBench

    jobsFile, connectionConfiguration = _loadDataJobs(arguments)
    jobs = {name: job for name, job in _selectJobs(jobsFile.jobs, arguments.job, log).items() if arguments.job or job.active}
    if not jobs:
        raise UsageError('no active jobs to measure; name one with --job')

    try:
        effectiveMaskingThreads(jobsFile.maskingThreads)
    except ValueError as error:
        raise ConfigurationError(str(error)) from None
    # As many as `run` would give a job running alone.
    threads = maskingThreadsFor(jobsFile.maskingThreads, 1)
    setMaskingThreads(threads)

    results = []
    for name, job in jobs.items():
        log.logging.info('{}: reading up to {:,} row(s) from {}'.format(name, arguments.rows, job.sourceConnection))
        results.append(benchJob(name, job, connectionConfiguration, arguments.rows))

    maskerLine = masker(threads)
    text = json.dumps(benchReport(results, maskerLine), indent=2) + '\n' if arguments.format == 'json' else renderBench(results, maskerLine)
    _writeOutput(text, arguments.output)

    return EXIT_JOBS_DID_NOT_SUCCEED if any(result.error is not None for result in results) else EXIT_SUCCESS
