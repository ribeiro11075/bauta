"""The `bauta` command: its parser and main(), one module per group of
commands beside it -- `run`, `generate`, `review` -- and what they share in
`common`.

The only place in the package that reads files and the environment; the
modules under it take already-loaded, validated objects.

Exit codes, which are the interface for anything that schedules work:

    0    every active job completed
    1    at least one job failed or was skipped, or the command itself failed
    2    invalid configuration, or a usage error
    130  interrupted
"""
from __future__ import annotations

import argparse
import os
import sys
from typing import Any, Optional, Sequence

from ..configuration import ConfigurationError
from ..log.scrubbing import describeError
from .common import (CONFIG_DIRECTORY_VARIABLE, EXIT_BAD_CONFIGURATION, EXIT_INTERRUPTED, EXIT_JOBS_DID_NOT_SUCCEED, EXIT_SUCCESS, MANIFEST_KEY_VARIABLE,
                     NOTIFY_URL_VARIABLE, UsageError, _configureLogging, _toolVersion)
from .generate import _commandClear, _commandDiscover, _commandSchema, _commandSubset, _commandSynthesize
from .review import _commandAudit, _commandCoverage, _commandVerifyReferences
from .run import _commandHistory, _commandJobs, _commandRun, _commandValidate, _commandVerifyManifest

__all__ = [
    'EXIT_BAD_CONFIGURATION',
    'EXIT_INTERRUPTED',
    'EXIT_JOBS_DID_NOT_SUCCEED',
    'EXIT_SUCCESS',
    'main',
    'UsageError',
    ]


def _addCommonArguments(parser: argparse.ArgumentParser, jobs: bool = True, memory: bool = True) -> None:

    parser.add_argument('--config', help='directory holding jobs.yaml and database.yaml (default: ${} or ./configuration)'.format(
        CONFIG_DIRECTORY_VARIABLE))
    if jobs:
        parser.add_argument('--jobs', help='explicit path to the jobs file, overriding --config')
    parser.add_argument('--databases', help='explicit path to the database file, overriding --config')
    if jobs and memory:
        parser.add_argument('--memory', help='path to the run-memory file (default: jobs.yaml\'s `memory`, else memory.yaml beside jobs.yaml)')
        parser.add_argument('--memory-database', metavar='ALIAS',
                            help='keep run memory in this database instead of a file (see docs/operations.md)')
        parser.add_argument('--memory-table', help='the run-memory table (default: jobs.yaml\'s, else bauta_memory)')
    _addLoggingArguments(parser)


def _addHistoryArguments(parser: argparse.ArgumentParser) -> None:

    parser.add_argument('--history', metavar='FILE', help='run history as JSON lines (default: jobs.yaml\'s `history`)')
    parser.add_argument('--history-database', metavar='ALIAS', help='run history in this database')
    parser.add_argument('--history-table', help='the history table (default: jobs.yaml\'s, else bauta_history)')


def _addManifestLocationArguments(parser: argparse.ArgumentParser) -> None:

    parser.add_argument('--manifest-database', metavar='ALIAS', help='the masking manifest in this database')
    parser.add_argument('--manifest-table', help='the manifest table (default: jobs.yaml\'s, else bauta_manifest)')


def _addManifestKeyArgument(parser: argparse.ArgumentParser) -> None:

    parser.add_argument('--manifest-key-variable', default=MANIFEST_KEY_VARIABLE,
                        help='environment variable holding the manifest signing key (default: {})'.format(MANIFEST_KEY_VARIABLE))


def _addLoggingArguments(parser: argparse.ArgumentParser) -> None:

    parser.add_argument('--log', help='also write logs to this file (logs always go to stderr unless --quiet)')
    parser.add_argument('--log-level', default='info', choices=['debug', 'info', 'warning', 'error'], help='default: info')
    parser.add_argument('--log-format', default='text', choices=['text', 'json'],
                        help='json emits one object per record, carrying job/status/rowCount as fields a log collector can filter and alert on')
    parser.add_argument('--quiet', action='store_true', help='do not log to stderr')


def _positiveInteger(text: str) -> int:

    value = int(text)
    if value < 1:
        raise argparse.ArgumentTypeError('must be at least 1, got {}'.format(value))

    return value


def _addRunArguments(parser: argparse.ArgumentParser) -> None:

    parser.add_argument('--forever', action='store_true',
                        help='keep running, honoring each job\'s refresh window. Prefer a single run from cron or a CronJob; '
                             'use this only for freshness below cron\'s one-minute floor, or where there is no scheduler')
    parser.add_argument('--job', action='append', help='run only this job (repeatable). Implies --force, and does NOT run its predecessors')
    parser.add_argument('--force', action='store_true', help='ignore refresh windows')
    parser.add_argument('--workers', type=_positiveInteger, help='override the worker count from configuration')
    parser.add_argument('--dry-run', action='store_true',
                        help='check connections, target tables, primary keys and masking coverage without moving any rows')


def _addRulesArgument(parser: argparse.ArgumentParser) -> None:

    parser.add_argument('--rules', metavar='FILE', help='your own rules for recognising personal data, ahead of the built-in ones '
                                                        '(default: discovery.yaml in the configuration directory, if there is one)')


def _addGeneratorArguments(parser: argparse.ArgumentParser) -> None:

    parser.add_argument('--sample', type=_positiveInteger, default=1000, help='rows sampled per table to classify columns (default: 1000)')
    parser.add_argument('--key-variable', default='MASKING_KEY', help='environment variable the generated jobs read the masking key from')
    parser.add_argument('--chunk-size', type=_positiveInteger, default=5000, help='chunkSize for the generated jobs (default: 5000)')
    parser.add_argument('--mask-keys', action='store_true',
                        help='mask numeric surrogate keys too, in the domain each foreign key shares, instead of keeping them')
    parser.add_argument('--output', help='write the generated jobs here instead of stdout; must not already exist')


class _PrintVersion(argparse.Action):
    """`bauta --version`: this package's version, and which masker it would
    use -- the two things anyone helping with a problem asks first.
    """

    def __init__(self, option_strings: Sequence[str], dest: str = argparse.SUPPRESS, default: Any = argparse.SUPPRESS,
                 help: Optional[str] = None) -> None:
        super().__init__(option_strings, dest=dest, default=default, nargs=0, help=help)

    def __call__(self, parser: argparse.ArgumentParser, namespace: argparse.Namespace, values: Any, option_string: Optional[str] = None) -> None:
        from ..masking import nativeVersion

        native = nativeVersion()
        if native:
            masker = 'bauta-rs {}'.format(native)
        elif os.environ.get('BAUTA_NATIVE') == '0':
            masker = 'python (BAUTA_NATIVE=0 turns the native masker off)'
        else:
            masker = 'python (pip install "bauta[native]" for the native masker)'
        print('bauta {}'.format(_toolVersion()))
        print('masking: {}'.format(masker))
        parser.exit()


def _buildParser() -> argparse.ArgumentParser:

    parser = argparse.ArgumentParser(prog='bauta', description='Move, mask and subset data between databases, with jobs defined in YAML.')
    parser.add_argument('--version', action=_PrintVersion, help='print the version, and which masker it would use')
    subparsers = parser.add_subparsers(dest='command', required=True)

    runParser = subparsers.add_parser('run', help='run data jobs')
    _addCommonArguments(runParser)
    _addRunArguments(runParser)
    runParser.add_argument('--manifest', metavar='FILE',
                           help='write a JSON record of what was masked, how, and under which key fingerprint (default: jobs.yaml\'s `manifest`)')
    _addManifestLocationArguments(runParser)
    _addManifestKeyArgument(runParser)
    runParser.add_argument('--accept-key-change', action='store_true',
                           help='run upsert jobs even though their masking key changed since their last run')
    _addHistoryArguments(runParser)
    runParser.add_argument('--notify-url', metavar='URL', help='post a JSON summary to this webhook when a cycle does not succeed '
                                                              '(default: ${})'.format(NOTIFY_URL_VARIABLE))
    runParser.add_argument('--notify-on', default='failure', choices=['failure', 'always'], help='default: failure')
    runParser.set_defaults(handler=_commandRun)

    validateParser = subparsers.add_parser('validate', help='check configuration offline, without connecting to anything')
    _addCommonArguments(validateParser)
    _addRulesArgument(validateParser)
    validateParser.set_defaults(handler=_commandValidate)

    auditParser = subparsers.add_parser('audit', help='report what each job does with data, and what a reviewer should question')
    # Reads the jobs, never run state, so no --memory flags.
    _addCommonArguments(auditParser, memory=False)
    auditParser.add_argument('--connect', action='store_true',
                             help='also run each masked query for its real columns, and check whether each connection is encrypted')
    auditParser.add_argument('--job', action='append', help='audit only this job (repeatable)')
    auditParser.add_argument('--format', default='text', choices=['text', 'json'], help='default: text')
    auditParser.add_argument('--strict', action='store_true', help='exit 1 on warnings as well as errors')
    auditParser.add_argument('--output', help='write the report here instead of stdout; must not already exist')
    _addRulesArgument(auditParser)
    auditParser.set_defaults(handler=_commandAudit)

    referencesParser = subparsers.add_parser('verify-references', help='count rows in each target whose foreign key points at nothing')
    _addCommonArguments(referencesParser, memory=False)
    referencesParser.add_argument('--job', action='append', help='check only this job\'s target table (repeatable)')
    referencesParser.add_argument('--format', default='text', choices=['text', 'json'], help='default: text')
    referencesParser.add_argument('--output', help='write the report here instead of stdout; must not already exist')
    referencesParser.set_defaults(handler=_commandVerifyReferences)

    coverageParser = subparsers.add_parser('coverage', help='list a source database\'s tables and what the jobs do with each')
    _addCommonArguments(coverageParser, memory=False)
    _addRulesArgument(coverageParser)
    coverageParser.add_argument('--database', help='the source database alias to check; required when the jobs read from more than one')
    coverageParser.add_argument('--schema', help='the schema to list, instead of the connection\'s own')
    coverageParser.add_argument('--job', action='append', help='only these jobs count as covering a table. Repeatable.')
    coverageParser.add_argument('--format', choices=['text', 'json'], default='text', help='output format')
    coverageParser.add_argument('--output', help='write to this file instead of stdout')

    verifyParser = subparsers.add_parser('verify-manifest', help='check that a manifest is unaltered, and who signed it')
    verifyParser.add_argument('manifest', nargs='?', help='a manifest file (default: --manifest-database, else jobs.yaml\'s `manifest`)')
    _addManifestLocationArguments(verifyParser)
    verifyParser.add_argument('--run', metavar='RUN_ID', help='from a table, this run\'s manifest rather than the latest')
    _addCommonArguments(verifyParser, memory=False)
    _addManifestKeyArgument(verifyParser)
    verifyParser.set_defaults(handler=_commandVerifyManifest)

    historyParser = subparsers.add_parser('history', help='show recent job outcomes recorded with run --history')
    _addCommonArguments(historyParser, memory=False)
    _addHistoryArguments(historyParser)
    historyParser.add_argument('--job', help='only this job')
    historyParser.add_argument('--limit', type=_positiveInteger, default=20, help='how many records (default: 20)')
    historyParser.add_argument('--format', default='text', choices=['text', 'json'], help='default: text')
    historyParser.set_defaults(handler=_commandHistory)

    jobsParser = subparsers.add_parser('jobs', help='show the job graph and which jobs are due')
    _addCommonArguments(jobsParser)
    jobsParser.set_defaults(handler=_commandJobs)

    discoverParser = subparsers.add_parser('discover', help='propose a masking policy for tables, from their schema and a sample')
    _addCommonArguments(discoverParser, jobs=False)
    discoverParser.add_argument('--database', required=True, help='the alias to read from')
    discoverParser.add_argument('--table', action='append', help='a table to propose a policy for (repeatable)')
    discoverParser.add_argument('--all-tables', action='store_true', help='every table in the database, instead of naming each with --table')
    discoverParser.add_argument('--schema', help='the schema --all-tables lists, instead of the connection\'s own')
    discoverParser.add_argument('--target', help='the alias the generated jobs load into (default: --database, masking in place)')
    _addGeneratorArguments(discoverParser)
    _addRulesArgument(discoverParser)
    discoverParser.set_defaults(handler=_commandDiscover)

    subsetParser = subparsers.add_parser('subset', help='generate jobs that copy a referentially complete subset')
    _addCommonArguments(subsetParser, jobs=False)
    subsetParser.add_argument('--database', required=True, help='the alias to read from')
    subsetParser.add_argument('--target', required=True, help='the alias the generated jobs load into')
    subsetParser.add_argument('--root', required=True, help='the table the subset starts from')
    subsetParser.add_argument('--where', required=True, help='SQL filter on the root table, e.g. "created_at >= \'2026-01-01\'"')
    subsetParser.add_argument('--no-children', action='store_true', help='copy only the root rows and what they reference, not rows referencing them')
    subsetParser.add_argument('--ignore-foreign-key', action='append', metavar='TABLE.COLUMN',
                              help='do not follow this foreign key (repeatable); needed to break a cycle')
    subsetParser.add_argument('--mask', action='store_true', help='also propose a masking policy for every table, as discover does')
    _addGeneratorArguments(subsetParser)
    _addRulesArgument(subsetParser)
    subsetParser.set_defaults(handler=_commandSubset)

    schemaParser = subparsers.add_parser('schema', help='generate or apply CREATE TABLE statements for a target, from source tables')
    _addCommonArguments(schemaParser, jobs=False)
    schemaParser.add_argument('--database', required=True, help='the alias to read table definitions from')
    schemaParser.add_argument('--target', required=True, help='the alias the tables are for; its dialect decides the types')
    schemaParser.add_argument('--table', action='append', required=True, help='a table to create (repeatable)')
    schemaParser.add_argument('--related', action='store_true', help='also every table a subset rooted at --table would copy')
    schemaParser.add_argument('--no-children', action='store_true', help='with --related, only the tables --table references')
    schemaParser.add_argument('--no-foreign-keys', action='store_true', help='leave foreign keys out of the generated tables')
    schemaParser.add_argument('--stage-suffix', help='also create <table><suffix> stage tables, for swap jobs; alone if --target is --database')
    schemaParser.add_argument('--apply', action='store_true', help='create the tables in --target, skipping any that already exist')
    schemaParser.add_argument('--output', help='write the SQL here instead of stdout; must not already exist')
    schemaParser.set_defaults(handler=_commandSchema)

    synthesizeParser = subparsers.add_parser('synthesize', help='fill existing tables with generated rows, for data that can\'t be copied')
    _addCommonArguments(synthesizeParser, jobs=False)
    synthesizeParser.add_argument('--database', required=True, help='the alias whose tables to fill')
    synthesizeParser.add_argument('--table', action='append', required=True, metavar='TABLE[:ROWS]',
                                  help='a table to fill, and how many rows (repeatable); parents are filled first')
    synthesizeParser.add_argument('--rows', type=_positiveInteger, default=100, help='rows for a --table without a count (default: 100)')
    synthesizeParser.add_argument('--seed', type=int, default=0, help='the same seed makes the same rows (default: 0)')
    synthesizeParser.add_argument('--dry-run', action='store_true', help='show what each column gets, and sample rows, without writing')
    synthesizeParser.add_argument('--yes', action='store_true', help='actually insert the rows')
    _addRulesArgument(synthesizeParser)
    synthesizeParser.set_defaults(handler=_commandSynthesize)

    clearParser = subparsers.add_parser('clear', help='empty the target tables of data jobs, children first (destructive)')
    _addCommonArguments(clearParser)
    clearParser.add_argument('--job', action='append', help='clear only this job\'s target (repeatable)')
    clearParser.add_argument('--dry-run', action='store_true', help='show which tables would be emptied, and in what order')
    clearParser.add_argument('--yes', action='store_true', help='actually delete the rows')
    clearParser.set_defaults(handler=_commandClear)
    coverageParser.set_defaults(handler=_commandCoverage)

    return parser


def _fillSharedFlags(parser: argparse.ArgumentParser, arguments: argparse.Namespace) -> None:
    """Sets every flag some other subcommand defines, and this one doesn't, to
    None, so the helpers several subcommands share can read any of them
    without asking first. Read from the parser itself, so no list of names
    can fall out of step with the flags.
    """

    (subparsers,) = [action for action in parser._actions if isinstance(action, argparse._SubParsersAction)]

    for subparser in subparsers.choices.values():
        for action in subparser._actions:
            if action.dest != argparse.SUPPRESS and not hasattr(arguments, action.dest):
                setattr(arguments, action.dest, None)


def main(argv: Optional[Sequence[str]] = None) -> int:

    parser = _buildParser()
    arguments = parser.parse_args(argv)

    _fillSharedFlags(parser, arguments)

    log = _configureLogging(arguments)

    try:
        return int(arguments.handler(arguments, log))
    except (ConfigurationError, UsageError) as error:
        log.logging.error(str(error))
        return EXIT_BAD_CONFIGURATION
    except KeyboardInterrupt:
        log.logging.warning('Interrupted')
        return EXIT_INTERRUPTED
    except Exception as error:
        # Logged, rather than left to Python's own traceback, so a driver
        # error's quoted values are scrubbed on the way out.
        log.logging.error('Failed: {}'.format(describeError(error)), exc_info=error)
        return EXIT_JOBS_DID_NOT_SUCCEED


if __name__ == '__main__':
    sys.exit(main())
