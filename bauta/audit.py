"""What a set of jobs does with data, for a reviewer -- `bauta audit`.

Findings a valid policy can still deserve -- an `email` column kept as it is,
a defaultStrategy of `keep` -- for a person, or with --strict a CI gate.
Works on plain data; the CLI supplies whatever needs a connection.
"""
from __future__ import annotations

import datetime
import re
from typing import Any, Dict, Iterable, List, Mapping, NamedTuple, Optional, Sequence, Set, Tuple

from .configuration import DataJobConfig
from .databaseDialects import ForeignKey, bareName, unqualifiedName
from .discovery import BUILTIN_RULES, DiscoveryRules, personalDataHint
from .masking import MaskingError, MaskingPlan, keyFingerprint, resolveStrategy

SEVERITIES = ('error', 'warning', 'info')


class Finding(NamedTuple):

    severity: str
    job: Optional[str]
    message: str


class _Usage(NamedTuple):
    """How one job masks one column: what has to agree for masks to match.

    `domain` is None for a strategy that doesn't use the key, and `policy` is
    None for a column copied as it is -- `keep`, or a job that doesn't mask.
    """

    job: str
    column: str
    domain: Optional[str]
    policy: Optional[str]
    keyFingerprint: Optional[str]

    @property
    def label(self) -> str:

        return '{}.{}'.format(self.job, self.column)

    @property
    def signature(self) -> Tuple[Optional[str], Optional[str], Optional[str]]:

        return self.domain, self.policy, self.keyFingerprint

    def describe(self) -> str:

        if self.policy is None:
            return 'not masked'
        if self.domain is None:
            return 'masked with {}'.format(self.policy)

        return 'masked with {} in domain {} under key {}'.format(self.policy, self.domain, self.keyFingerprint)


def _describePolicy(policy: Mapping[str, Any]) -> str:
    """A strategy and its options, as a reviewer would compare them."""

    options = ', '.join('{}: {}'.format(name, policy[name]) for name in sorted(policy) if name not in ('strategy', 'domain'))

    return '{} ({})'.format(policy['strategy'], options) if options else policy['strategy']


def _usages(name: str, plan: MaskingPlan, columns: Sequence[Mapping[str, Any]]) -> List[_Usage]:

    fingerprint = keyFingerprint(plan.key)
    declared = {column.upper(): policy for column, policy in plan.columns.items()}
    usages = []

    for entry in columns:
        policy = declared.get(entry['column'].upper()) if entry['source'] == 'column' else plan.defaultStrategy
        assert policy is not None
        if policy['strategy'] == 'keep':
            usages.append(_Usage(name, entry['column'], None, None, None))
        elif entry['domain'] is None:
            usages.append(_Usage(name, entry['column'], None, _describePolicy(policy), None))
        else:
            usages.append(_Usage(name, entry['column'], entry['domain'], _describePolicy(policy), fingerprint))

    return usages


def _labels(usages: Iterable[_Usage]) -> str:

    return ', '.join(sorted(usage.label for usage in usages))


def _auditDomains(target: str, usages: Sequence[_Usage], findings: List[Finding]) -> None:
    """Masks agree only between columns masked in the same domain, the same way,
    under the same key. A domain shared in one target database is a promise that
    they do, so each difference is reported. Copies in different target
    databases may deliberately use different keys, so they aren't compared.
    """

    byDomain: Dict[str, List[_Usage]] = {}
    for usage in usages:
        if usage.domain is not None:
            byDomain.setdefault(usage.domain, []).append(usage)

    for domain, shared in sorted(byDomain.items()):
        for what, attribute, noun in (('under {} different keys', 'keyFingerprint', 'key {}'), ('{} different ways', 'policy', '{}')):
            variants: Dict[str, List[_Usage]] = {}
            for usage in shared:
                variants.setdefault(getattr(usage, attribute), []).append(usage)
            if len(variants) < 2:
                continue
            findings.append(Finding('warning', None, 'in {}, domain {} is masked {}, so its masks cannot match across them: {}. '
                                    'Mask the domain one way, or give columns that should not match a domain of their own'.format(
                                        target, domain, what.format(len(variants)),
                                        '; '.join('{} for {}'.format(noun.format(variant), _labels(group)) for variant, group in sorted(variants.items())))))


def _auditForeignKeys(target: str, jobs: Mapping[str, DataJobConfig], usagesByJob: Mapping[str, Sequence[_Usage]],
                      targetColumns: Mapping[str, Sequence[str]], foreignKeys: Sequence[ForeignKey], findings: List[Finding]) -> None:
    """A foreign key survives masking only if its columns are masked exactly as
    the columns they reference. Jobs are matched to a key's tables by their
    targetTableFinal's name, and a masked job's target columns to its query's
    by position, as the load matches them.
    """

    # (table, column) -> how each job loading that table fills that column
    filled: Dict[Tuple[str, str], List[_Usage]] = {}
    for name, job in sorted(jobs.items()):
        table = _tableName(job.targetTableFinal).upper()
        if job.masking is None:
            filled.setdefault((table, '*'), []).append(_Usage(name, '*', None, None, None))
            continue
        columns = targetColumns.get(name)
        usages = usagesByJob.get(name)
        if columns is None or usages is None or len(columns) != len(usages):
            continue
        for column, usage in zip(columns, usages):
            filled.setdefault((table, column.upper()), []).append(usage)

    def lookup(table: str, column: str) -> List[_Usage]:
        return filled.get((table.upper(), column.upper()), []) + filled.get((table.upper(), '*'), [])

    for foreignKey in foreignKeys:
        for column, referencedColumn in zip(foreignKey.columns, foreignKey.referencedColumns):
            for child in lookup(foreignKey.table, column):
                for parent in lookup(foreignKey.referencedTable, referencedColumn):
                    # A NULL reference points at nothing, so it can't break.
                    if child.policy == 'null':
                        continue
                    # shuffle rearranges values between rows rather than mapping
                    # them, so two columns shuffled alike still stop matching.
                    if child.signature == parent.signature:
                        if child.policy and child.policy.startswith('shuffle'):
                            findings.append(Finding('warning', child.job, 'in {}, {}.{} and {}.{}, which it references, are both masked with '
                                                    'shuffle, which moves values between rows rather than mapping them, so the copied '
                                                    'references will point at other rows. Mask a key and its references with key or fpe'.format(
                                                        target, foreignKey.table, column, foreignKey.referencedTable, referencedColumn)))
                        continue
                    findings.append(Finding('warning', child.job, 'in {}, {}.{} is {}, but {}.{}, which it references, is {} (by {}), '
                                            'so the copied references will not match'.format(
                                                target, foreignKey.table, column, child.describe(),
                                                foreignKey.referencedTable, referencedColumn, parent.describe(), parent.job)))


# What a query says when it returns some of a table's rows, as far as reading
# it without parsing SQL can tell. A join usually narrows too, and saying so is
# worth the occasional job that joins only to widen its columns.
_PARTIAL_QUERIES = (
    (r'\bwhere\b', 'its sourceQuery has a WHERE'),
    (r'\blimit\b', 'its sourceQuery has a LIMIT'),
    (r'\btop\s*\(?\s*\d', 'its sourceQuery has a TOP'),
    (r'\bfetch\s+first\b', 'its sourceQuery has a FETCH FIRST'),
    (r'\bjoin\b', 'its sourceQuery joins another table'),
    )


def _partial(job: DataJobConfig) -> Optional[str]:
    """Why a job copies only part of its table, or None if it looks whole."""

    if job.watermarkColumn:
        return 'incremental on {}'.format(job.watermarkColumn)

    for pattern, reason in _PARTIAL_QUERIES:
        if re.search(pattern, job.sourceQuery, re.IGNORECASE):
            return reason

    return None


def _tableName(table: str) -> str:
    """A target table as audit matches it against a catalog's foreign keys:
    without its schema, and without the quotes a reserved word or a mixed-case
    name needs in a job, since a catalog reports names bare.
    """

    return bareName(unqualifiedName(table))


def _mentions(query: str, table: str) -> bool:

    return re.search(r'(?<![\w$]){}(?![\w$])'.format(re.escape(_tableName(table))), query, re.IGNORECASE) is not None


def _auditCoverage(target: str, jobs: Mapping[str, DataJobConfig], foreignKeys: Sequence[ForeignKey], findings: List[Finding]) -> None:
    """A copy keeps its references only if every row it copies points at a row
    it also copies. A parent copied in part breaks that unless the child's query
    is limited by the parent (subset's EXISTS), or the parent's query takes in
    what the child references. Either shows as one query naming the other's
    table, which is as far as a check that doesn't parse SQL can see.
    """

    byTable: Dict[str, List[Tuple[str, DataJobConfig]]] = {}
    for name, job in sorted(jobs.items()):
        byTable.setdefault(_tableName(job.targetTableFinal).upper(), []).append((name, job))

    for foreignKey in foreignKeys:
        # A table referencing itself is one job; subset reports such cycles.
        if foreignKey.table.upper() == foreignKey.referencedTable.upper():
            continue
        for parentName, parent in byTable.get(foreignKey.referencedTable.upper(), []):
            reason = _partial(parent)
            if reason is None or _mentions(parent.sourceQuery, foreignKey.table):
                continue
            for childName, child in byTable.get(foreignKey.table.upper(), []):
                if _mentions(child.sourceQuery, foreignKey.referencedTable):
                    continue
                findings.append(Finding('warning', childName, 'in {target}, {child}.{columns} references {parent}, which {parentJob} copies only '
                                        'in part ({reason}), but {childJob} is not limited to the rows it copies, so the copy can reference '
                                        'rows it lacks. Limit {childJob} to rows whose {parent} {parentJob} copies, have {parentJob} also '
                                        'select what {childJob} references, or copy {parent} whole'.format(
                                            target=target, child=foreignKey.table, columns=', '.join(foreignKey.columns),
                                            parent=foreignKey.referencedTable, parentJob=parentName, childJob=childName, reason=reason)))


def _waitedFor(jobs: Mapping[str, DataJobConfig], name: str, slowest: Optional[int]) -> Tuple[Set[str], bool]:
    """(the jobs `name` waits for, whether any predecessor on the way isn't in
    `jobs`). A run drops inactive predecessors, and each cycle drops those
    inside their refresh window, so the chain stops at both. `slowest`, when
    given, drops those with a longer refresh.
    """

    waited: Set[str] = set()
    unknown = False
    pending = list(jobs[name].predecessors)
    while pending:
        predecessor = pending.pop()
        if predecessor in waited:
            continue
        job = jobs.get(predecessor)
        if job is None:
            unknown = True
            continue
        if not job.active or (slowest is not None and (job.refresh or 0) > slowest):
            continue
        waited.add(predecessor)
        pending.extend(job.predecessors)

    return waited, unknown


def _auditOrdering(target: str, jobs: Mapping[str, DataJobConfig], foreignKeys: Sequence[ForeignKey], findings: List[Finding]) -> None:
    """A child loaded before its parent references parent rows not loaded yet.
    Whether it waits is decided as a run decides it (DependencyGraph): through
    active predecessors, in the cycles they run in.
    """

    byTable: Dict[str, List[str]] = {}
    for name, job in sorted(jobs.items()):
        byTable.setdefault(_tableName(job.targetTableFinal).upper(), []).append(name)

    for foreignKey in foreignKeys:
        if foreignKey.table.upper() == foreignKey.referencedTable.upper():
            continue
        for childName in byTable.get(foreignKey.table.upper(), []):
            child = jobs[childName]
            if not child.active:
                continue
            everyCycle, _ = _waitedFor(jobs, childName, child.refresh or 0)
            sometimes, unknown = _waitedFor(jobs, childName, None)
            where = 'in {}, {}.{} references {}'.format(target, foreignKey.table, ', '.join(foreignKey.columns), foreignKey.referencedTable)
            for parentName in byTable.get(foreignKey.referencedTable.upper(), []):
                parent = jobs[parentName]
                if parentName == childName or parentName in everyCycle:
                    continue
                if not parent.active:
                    message = '{}, but {}, which loads it, is inactive, so {} loads rows whose {} are not loaded'.format(
                        where, parentName, childName, foreignKey.referencedTable)
                elif parentName in sometimes:
                    # The jobs on the way to the parent whose refresh breaks the chain.
                    slow = sorted(name for name in sometimes if (jobs[name].refresh or 0) > (child.refresh or 0)
                                  and (name == parentName or parentName in _waitedFor(jobs, name, None)[0]))
                    message = ('{}, and {} waits for {} only in cycles that {} run in, since their refresh is longer than {}\'s, so '
                               'in the others it can load rows whose {} are not loaded yet. Give them the same refresh'.format(
                                   where, childName, parentName, ', '.join(slow), childName, foreignKey.referencedTable))
                elif unknown:
                    continue
                else:
                    message = ('{}, but {} does not wait for {}, directly or through its other predecessors, so it can load rows whose '
                               '{} are not loaded yet. Add {} to its predecessors'.format(
                                   where, childName, parentName, foreignKey.referencedTable, parentName))
                findings.append(Finding('warning', childName, message))


def _auditSwaps(target: str, jobs: Mapping[str, DataJobConfig], declared: Sequence[ForeignKey], findings: List[Finding]) -> None:
    """A key follows the table it was declared on, not its name, so a swap
    leaves another table's key on the old table, now the stage. Every dialect
    then refuses to empty the stage for the next run: TRUNCATE a referenced
    table, or SQLite's DELETE of rows still referenced. Only keys the target
    declares matter; a source's constrain nothing here. A job whose
    postTargetAdhocQueries name the referencing table is taken to recreate them.
    """

    for name, job in sorted(jobs.items()):
        if job.insertStrategy != 'swap' or not job.targetTableStage:
            continue
        final, stage = _tableName(job.targetTableFinal), _tableName(job.targetTableStage)
        for foreignKey in declared:
            referenced = foreignKey.referencedTable.upper()
            if foreignKey.table.upper() in (referenced, final.upper(), stage.upper()) or referenced not in (final.upper(), stage.upper()):
                continue
            if any(_mentions(query, foreignKey.table) for query in job.postTargetAdhocQueries):
                continue
            where = 'in {}, {}.{} references {}'.format(target, foreignKey.table, ', '.join(foreignKey.columns), foreignKey.referencedTable)
            if referenced == final.upper():
                what = ('which {} replaces by swap. The key stays on the table it was declared on, which the swap renames to {}, so it '
                        'stops checking {}'.format(name, stage, final))
            else:
                what = 'the stage table of {}, as an earlier swap leaves it, so it does not check {}'.format(name, final)
            findings.append(Finding('error', name, '{}, {}, and the next run cannot empty {}. Recreate the key on {} in postTargetAdhocQueries, '
                                    'or load {} with upsert and a stage table'.format(where, what, stage, final, final)))

        # The other side: the swapped table's own keys stay on the old table,
        # and the stage that replaces it has none, so the copy stops enforcing
        # them until the next swap brings the original back.
        own = sorted({(foreignKey.table, ', '.join(foreignKey.columns), foreignKey.referencedTable) for foreignKey in declared
                      if foreignKey.table.upper() == final.upper() and foreignKey.referencedTable.upper() != final.upper()})
        if own and not any(_mentions(query, final) for query in job.postTargetAdhocQueries):
            findings.append(Finding('warning', name, 'in {}, {} declares foreign key(s) {}, but {} replaces it by swap with {}, which declares '
                                    'none, so after a run the copy stops enforcing them. Recreate them on {} in postTargetAdhocQueries, or load '
                                    'it with upsert and a stage table'.format(
                                        target, final, '; '.join('{} -> {}'.format(columns, parent) for _, columns, parent in own),
                                        name, stage, final)))


def _declaredColumns(plan: MaskingPlan) -> List[Dict[str, Any]]:
    """The policy as written, for a job whose query wasn't run."""

    columns = []
    for column, policy in plan.columns.items():
        keyed = resolveStrategy(policy['strategy']).KEYED
        columns.append({'column': column, 'strategy': policy['strategy'], 'domain': policy.get('domain', column.lower()) if keyed else None,
                        'source': 'column'})

    return columns


def _auditMaskedJob(name: str, job: DataJobConfig, returned: Optional[Sequence[str]], findings: List[Finding],
                    usages: Dict[str, List[_Usage]], rules: DiscoveryRules) -> Dict[str, Any]:

    assert job.masking is not None
    plan = MaskingPlan(key=job.masking.key.get_secret_value(), columns=job.masking.columns, defaultStrategy=job.masking.defaultStrategy)
    columns = _declaredColumns(plan)
    resolved = False

    if returned is not None:
        try:
            columns = [entry._asdict() for entry in plan.bind(returned).manifest]
            resolved = True
        except MaskingError as error:
            findings.append(Finding('error', name, 'the policy does not match what sourceQuery returns: {}'.format(error)))

    for entry in columns:
        hint = personalDataHint(entry['column'], rules)
        entry['personalDataHint'] = hint
        if entry['strategy'] == 'keep' and hint:
            findings.append(Finding('warning', name, 'column {} is kept unmasked, but its {}'.format(entry['column'], hint)))

    defaultStrategy = plan.defaultStrategy
    if defaultStrategy is not None:
        if defaultStrategy['strategy'] == 'keep':
            findings.append(Finding('warning', name, 'defaultStrategy is keep, so any column added to the source later is copied unmasked'))
        fallen = [entry['column'] for entry in columns if entry['source'] == 'defaultStrategy']
        if fallen:
            findings.append(Finding('info', name, '{} column(s) fall to defaultStrategy {}: {}'.format(
                len(fallen), defaultStrategy['strategy'], ', '.join(fallen))))

    redacted = sorted(column for column, policy in plan.columns.items() if policy['strategy'] == 'redact')
    if redacted:
        findings.append(Finding('info', name, 'redact on {}: identifiers with a recognisable shape are removed, names are not'.format(
            ', '.join(redacted))))

    lenient = sorted(column for column, policy in plan.columns.items() if policy['strategy'] == 'fpe' and not policy.get('strict'))
    if lenient:
        findings.append(Finding('info', name, 'fpe without strict on {}: values too short for FF1 are masked with key instead'.format(
            ', '.join(lenient))))

    usages[name] = _usages(name, plan, columns)

    watermarked = [entry for entry in columns if job.watermarkColumn and entry['column'].upper() == job.watermarkColumn.upper()]
    if any(entry['strategy'] != 'keep' for entry in watermarked):
        findings.append(Finding('error', name, 'watermarkColumn {} falls to defaultStrategy {}, and the watermark is read before masking and '
                                'kept in run state and logs, so it would leak the unmasked value'.format(
                                    job.watermarkColumn, watermarked[0]['strategy'])))

    if job.watermarkColumn and any(entry['strategy'] == 'shuffle' for entry in columns):
        findings.append(Finding('warning', name, 'shuffle on an incremental job: its small chunks leave values on or near their own rows'))

    return {
        'keyFingerprint': keyFingerprint(job.masking.key.get_secret_value()),
        'defaultStrategy': defaultStrategy,
        'columnsResolved': resolved,
        'columns': columns,
        }


def _auditUnmaskedJob(name: str, job: DataJobConfig, returned: Optional[Sequence[str]], maskedSources: Set[str],
                      findings: List[Finding], rules: DiscoveryRules) -> None:
    """A job with no masking policy.

    Copying unmasked is a choice a reviewer has to see, so it is a finding
    unless the job declares it with `unmasked`, which the report records
    instead. A column that looks like personal data is still called out even
    then -- declared, as a warning; undeclared, as an error.

    Nothing here depends on another job masking the same source: the first
    table copied from a new source is exactly the case that matters.
    """

    personal = [(column, personalDataHint(column, rules)) for column in (returned or [])]
    personal = [(column, hint) for column, hint in personal if hint]

    if personal:
        findings.append(Finding('warning' if job.unmasked else 'error', name,
                                'copies from {} without masking, and {} it returns {} like personal data: {}. '
                                'Add a masking policy naming every column -- `keep` for the ones that need no masking'.format(
                                    job.sourceDatabase, 'a column' if len(personal) == 1 else 'columns',
                                    'looks' if len(personal) == 1 else 'look',
                                    ', '.join('{} ({})'.format(column, hint) for column, hint in personal))))
        return

    if job.unmasked:
        return

    if job.sourceDatabase in maskedSources and job.sourceDatabase != job.targetDatabase:
        findings.append(Finding('warning', name, 'copies from {} without masking, though other jobs mask what they read from it'.format(
            job.sourceDatabase)))
        return

    findings.append(Finding('warning', name, 'copies from {} to {} without masking, so every column it returns is written as it stands. '
                            'Add a masking policy, or declare the choice with `unmasked: true`'.format(
                                job.sourceDatabase, job.targetDatabase)))


def auditJobs(jobs: Mapping[str, DataJobConfig], returnedColumns: Optional[Mapping[str, Sequence[str]]] = None,
              encryption: Optional[Mapping[str, Optional[bool]]] = None, unreachable: Optional[Mapping[str, str]] = None,
              targetColumns: Optional[Mapping[str, Sequence[str]]] = None, foreignKeys: Optional[Mapping[str, Sequence[ForeignKey]]] = None,
              declaredForeignKeys: Optional[Mapping[str, Sequence[ForeignKey]]] = None,
              generatedAt: Optional[datetime.datetime] = None, rules: DiscoveryRules = BUILTIN_RULES) -> Dict[str, Any]:
    """The audit report, as a JSON-ready dict.

    `returnedColumns` maps a masked job to the columns its query returns, so
    each column's actual policy can be shown -- defaultStrategy included --
    rather than only the declared ones. `encryption` maps a database alias to
    whether its connection is encrypted (None: couldn't tell). `unreachable`
    maps a job to why its query couldn't be checked. `targetColumns` maps a
    masked job to its target's columns in load order, and `foreignKeys` maps a
    target database alias to the foreign keys that apply to its tables, for
    checking that references still match once masked. `declaredForeignKeys`
    maps a target database alias to the foreign keys it declares itself, for
    checking what a swap does to them. All of these come from connecting, and
    all are optional.
    """

    returnedColumns = returnedColumns or {}
    encryption = encryption or {}
    unreachable = unreachable or {}
    findings: List[Finding] = []
    usages: Dict[str, List[_Usage]] = {}
    maskedSources = {job.sourceDatabase for job in jobs.values() if job.masking is not None}
    report = []

    for name, job in sorted(jobs.items()):
        entry: Dict[str, Any] = {'job': name, 'active': job.active, 'sourceDatabase': job.sourceDatabase, 'targetDatabase': job.targetDatabase,
                                 'targetTable': job.targetTableFinal, 'masked': job.masking is not None,
                                 'unmasked': job.unmasked}

        if name in unreachable:
            findings.append(Finding('error', name, 'sourceQuery could not be checked: {}'.format(unreachable[name])))

        if job.masking is not None:
            entry.update(_auditMaskedJob(name, job, returnedColumns.get(name), findings, usages, rules))
            if encryption.get(job.sourceDatabase) is False:
                findings.append(Finding('warning', name, 'reads unmasked data from {} over a connection that is not encrypted'.format(job.sourceDatabase)))
        else:
            _auditUnmaskedJob(name, job, returnedColumns.get(name), maskedSources, findings, rules)

        report.append(entry)

    for target in sorted({job.targetDatabase for job in jobs.values()}):
        targetJobs = {name: job for name, job in jobs.items() if job.targetDatabase == target}
        _auditDomains(target, [usage for name in sorted(targetJobs) for usage in usages.get(name, [])], findings)
        if foreignKeys and foreignKeys.get(target):
            _auditForeignKeys(target, targetJobs, usages, targetColumns or {}, foreignKeys[target], findings)
            _auditCoverage(target, targetJobs, foreignKeys[target], findings)
            _auditOrdering(target, targetJobs, foreignKeys[target], findings)
        if declaredForeignKeys and declaredForeignKeys.get(target):
            _auditSwaps(target, targetJobs, declaredForeignKeys[target], findings)

    for alias, encrypted in sorted(encryption.items()):
        if encrypted is None and alias in maskedSources:
            findings.append(Finding('info', None, 'could not tell whether the connection to {} is encrypted'.format(alias)))

    findings.sort(key=lambda finding: (SEVERITIES.index(finding.severity), finding.job or '', finding.message))

    return {
        'generatedAt': (generatedAt or datetime.datetime.now(datetime.timezone.utc)).isoformat(timespec='seconds'),
        'jobs': report,
        'connections': {alias: {'encrypted': encrypted} for alias, encrypted in sorted(encryption.items())},
        'findings': [finding._asdict() for finding in findings],
        'summary': {severity: sum(1 for finding in findings if finding.severity == severity) for severity in SEVERITIES},
        }


def renderAudit(report: Mapping[str, Any]) -> str:
    """The report for a terminal: each job's columns, then the findings."""

    lines = []

    for job in report['jobs']:
        state = '' if job['active'] else ' (inactive)'
        lines.append('{}{}: {} -> {}.{}'.format(job['job'], state, job['sourceDatabase'], job['targetDatabase'], job['targetTable']))

        if not job['masked']:
            lines.append('  not masked, declared with `unmasked`' if job.get('unmasked') else '  not masked')
            lines.append('')
            continue

        scope = 'as returned by sourceQuery' if job['columnsResolved'] else 'as declared (run with --connect to resolve)'
        lines.append('  masked under key {}, columns {}:'.format(job['keyFingerprint'], scope))
        for column in job['columns']:
            domain = ' in domain {}'.format(column['domain']) if column['domain'] else ''
            origin = ' (defaultStrategy)' if column['source'] == 'defaultStrategy' else ''
            lines.append('    {:<28} {}{}{}'.format(column['column'], column['strategy'], domain, origin))
        if job['defaultStrategy'] and not job['columnsResolved']:
            lines.append('    {:<28} {} (defaultStrategy)'.format('any other column', job['defaultStrategy']['strategy']))
        lines.append('')

    if report['connections']:
        lines.append('Connections:')
        for alias, connection in report['connections'].items():
            encrypted = {True: 'encrypted', False: 'NOT encrypted', None: 'encryption unknown'}[connection['encrypted']]
            lines.append('  {:<28} {}'.format(alias, encrypted))
        lines.append('')

    summary = report['summary']
    lines.append('Findings: {} error(s), {} warning(s), {} note(s)'.format(summary['error'], summary['warning'], summary['info']))
    for finding in report['findings']:
        lines.append('  {:<8} {}{}'.format(finding['severity'].upper(), '{}: '.format(finding['job']) if finding['job'] else '', finding['message']))

    return '\n'.join(lines) + '\n'
