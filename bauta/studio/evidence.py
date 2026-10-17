"""A run's evidence, as one zip to hand a reviewer: what ran, the manifest
it sealed and whether that verifies, the audit and coverage as they stand,
and whether the configuration is still the one the run used -- with a
SHA-256 of every file in it, so a copy can be checked against the original.

Manifests are kept here by run, as Studio sees each one: a manifest file is
replaced by every run, and a run's evidence needs the one it wrote.
"""
from __future__ import annotations

import datetime
import hashlib
import io
import json
import os
import zipfile
from typing import Any, Dict, List, Optional, Tuple

from sqlalchemy import select
from sqlalchemy.orm import Session

from .. import verifyManifest
from ..project import Project
from .db import ArchivedManifest, Check

MANIFEST_KEY_VARIABLE = 'BAUTA_MANIFEST_KEY'


def manifestKey(manifest: Dict[str, Any]) -> str:
    """What a manifest is kept under: its run, or, written before manifests
    named their run, its digest.
    """

    runId = manifest.get('runId')
    if runId:
        return str(runId)

    return 'sha256:' + str((manifest.get('integrity') or {}).get('digest', ''))[:40]


def archiveManifest(session: Session, project: Project) -> Optional[str]:
    """Keeps the manifest `run` last wrote, if it isn't kept already.
    Returns what it was kept under, or None if there is none to keep.
    """

    try:
        found = project.readManifest()
    except (ValueError, OSError):
        return None
    if found is None:
        return None

    _, manifest = found
    key = manifestKey(manifest)
    if session.get(ArchivedManifest, key) is None:
        session.add(ArchivedManifest(runId=key, manifest=manifest))
        session.commit()

    return key


def manifestOf(session: Session, runId: str) -> Optional[Dict[str, Any]]:
    """The manifest a run wrote: kept under its run id, or, after a `run
    --job` merged it with others', one whose entries name it.
    """

    kept = session.get(ArchivedManifest, runId)
    if kept is not None:
        return kept.manifest

    for archived in session.scalars(select(ArchivedManifest).order_by(ArchivedManifest.archivedAt.desc()).limit(200)):
        if any(entry.get('runId') == runId for entry in archived.manifest.get('jobs', [])):
            return archived.manifest

    return None


def verification(manifest: Dict[str, Any]) -> Dict[str, Any]:

    signingKey = os.environ.get(MANIFEST_KEY_VARIABLE)
    try:
        result = verifyManifest(manifest, signingKey=signingKey)
    except ValueError as error:
        return {'verified': False, 'problem': str(error)}

    return {'digestValid': result.digestValid, 'signed': result.signed, 'signatureValid': result.signatureValid,
            'signingKeyFingerprint': result.signingKeyFingerprint, 'signatureChecked': result.signed and signingKey is not None,
            'verified': result.digestValid and (not result.signed or result.signatureValid)}


def _configurationNow(project: Project) -> List[Dict[str, str]]:

    return [{'file': str(path), 'sha256': hashlib.sha256(path.read_bytes()).hexdigest()} for path in project.document.files]


def _sameConfiguration(manifest: Optional[Dict[str, Any]], now: List[Dict[str, str]]) -> Optional[bool]:
    """Whether the jobs files are as they were when the manifest was written,
    by the digests it recorded; None when it recorded none.
    """

    recorded = (manifest or {}).get('configuration')
    if not recorded:
        return None
    then = [recorded.get('sha256')] + [entry.get('sha256') for entry in recorded.get('includes', [])]

    return then == [entry['sha256'] for entry in now]


def bundle(session: Session, project: Project, run: Dict[str, Any], version: str) -> Tuple[str, bytes]:
    """The run's evidence, as a file name and the bytes of a zip."""

    from ..review.html import renderAuditHtml, renderCoverageHtml

    generatedAt = datetime.datetime.now(datetime.timezone.utc).isoformat(timespec='seconds')
    manifest = manifestOf(session, run['runId'])
    configuration = _configurationNow(project)
    same = _sameConfiguration(manifest, configuration)
    audit = project.audit()
    coverage = session.scalars(select(Check).where(Check.kind == 'coverage').order_by(Check.id.desc()).limit(1)).first()

    files: Dict[str, bytes] = {}

    def add(name: str, content: Any) -> None:
        files[name] = (content if isinstance(content, str) else json.dumps(content, indent=2, default=str) + '\n').encode('utf-8')

    add('run.json', run)
    if manifest is not None:
        # Laid out afresh; its digest covers its content, not its layout, so
        # it verifies as the original does.
        add('manifest.json', manifest)
        add('manifest-verification.json', verification(manifest))
    add('configuration.json', {'files': configuration, 'sameAsTheRun': same})
    add('audit.json', audit)
    add('audit.html', renderAuditHtml(audit, version=version))
    if coverage is not None:
        add('coverage.json', dict(coverage.result['report'], checkedAt=coverage.at.isoformat(), checkedBy=coverage.email))
        add('coverage.html', renderCoverageHtml(coverage.result['report'], generatedAt=coverage.at.isoformat(), version=version))
    add('README.txt', _readme(run, manifest, same, coverage is not None, generatedAt, version))
    sums = ''.join('{}  {}\n'.format(hashlib.sha256(content).hexdigest(), name) for name, content in sorted(files.items()))
    files['SHA256SUMS.txt'] = sums.encode('ascii')

    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, 'w', zipfile.ZIP_DEFLATED) as archive:
        for name, content in files.items():
            archive.writestr(name, content)

    started = (run.get('startedAt') or generatedAt)[:19].replace(':', '').replace('-', '')

    return 'bauta-evidence-{}-{}.zip'.format(started, run['runId'][:8]), buffer.getvalue()


def _readme(run: Dict[str, Any], manifest: Optional[Dict[str, Any]], same: Optional[bool], hasCoverage: bool, generatedAt: str, version: str) -> str:

    counts = ', '.join('{} {}'.format(count, status) for status, count in sorted(run.get('counts', {}).items()))
    lines = [
        'Evidence for bauta run {}'.format(run['runId']),
        '',
        'Started {}, {} job(s): {}. {} row(s).'.format(run.get('startedAt'), run.get('jobCount'), counts, run.get('rowCount')),
        'Put together {} by bauta Studio {}.'.format(generatedAt, version),
        '',
        'run.json              what ran: each job, its outcome, rows, times and any error, as run history recorded it',
    ]
    if manifest is not None:
        lines += ['manifest.json         the masking manifest the run sealed: what was masked, how, under which key fingerprint',
                  'manifest-verification.json  whether its digest, and its signature if signed, still verify']
    else:
        lines += ['                      No manifest was kept for this run: the jobs file keeps none, or Studio never saw it.']
    lines += ['configuration.json    the jobs files now, with their SHA-256; sameAsTheRun says whether they match the manifest\'s',
              'audit.json, .html     the audit of the configuration as it is now, offline']
    if hasCoverage:
        lines += ['coverage.json, .html  the last coverage check: every table in the source, and what the jobs do with it']
    lines += ['SHA256SUMS.txt        the SHA-256 of every other file here: sha256sum -c SHA256SUMS.txt', '']
    if same is False:
        lines += ['NOTE: the jobs files have changed since this run. The audit describes the configuration as it is now, not as the run used it.', '']
    lines += ['Nothing here holds a value from any table, or a masking key.', '']

    return '\n'.join(lines)
