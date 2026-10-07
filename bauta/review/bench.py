"""`bauta bench`: where a job's time would go, measured against its real
source, writing nothing. See "Measure a job" in docs/guides/make-it-faster.md.

Each job's query is read, transformed and masked as `run` would, but in
turn rather than overlapped, so each stage's time is its own, and the rows
are dropped rather than written. The read limit on the source applies, so
measuring production takes no more of it than a run may.
"""
from __future__ import annotations

import time
from typing import Any, Dict, List, Mapping, NamedTuple, Optional

from ..configuration import ConnectionConfig, DataJobConfig
from ..database import Database
from ..log.scrubbing import describeError
from ..masking import core as maskingModule
from ..transform import Transform, resolveTransformer


class BenchResult(NamedTuple):
    """What reading `rows` rows of one job's query took. `complete` says the
    query had no more: `rows` is all of it. `error` is set, and the rest
    empty, for a job that couldn't be measured.
    """

    job: str
    rows: int = 0
    complete: bool = False
    connectSeconds: float = 0.0
    stages: Optional[Dict[str, float]] = None
    partitioned: bool = False
    error: Optional[str] = None

    def rate(self, stage: str) -> Optional[float]:
        """Rows a second through `stage`, or None where it took no time."""

        seconds = (self.stages or {}).get(stage, 0.0)

        return self.rows / seconds if seconds > 0 and self.rows else None

    @property
    def pace(self) -> Optional[str]:
        """The stage that would set a run's pace -- reading or masking, which a
        run overlaps -- or None for a job that read nothing.
        """

        if not self.stages or not self.rows:
            return None

        return 'masking' if self.stages['mask'] > self.stages['read'] else 'reading'


def benchJob(job: str, jobConfig: DataJobConfig, connectionConfiguration: Mapping[str, ConnectionConfig], rows: int) -> BenchResult:
    """Reads up to `rows` rows of `job`'s query, from where a first run would
    start, transforming and masking each chunk as it arrives and keeping
    none. Never raises for the job's own failure: the result carries it.
    """

    from ..jobs.pipeline import _bindMasking, _decodedJsonColumns, _preparer
    from ..jobs.throttle import StageTimes, readLimitFor, timedChunks

    times = StageTimes()
    settings = connectionConfiguration[jobConfig.sourceConnection]

    try:
        columnTransforms = {column: [resolveTransformer(reference) for reference in references]
                            for column, references in jobConfig.sourceQueryColumnTransforms.items()}

        started = time.perf_counter()
        with Database(connectionSettings=settings) as source:
            connectSeconds = time.perf_counter() - started

            query, parameters = jobConfig.sourceQuery, None
            if jobConfig.watermarkColumn:
                query, parameters = source.bindWatermark(query, jobConfig.watermarkInitial)

            started = time.perf_counter()
            columns, stream = source.stream(query=query, chunkSize=jobConfig.chunkSize, parameters=parameters)
            times.add('read', time.perf_counter() - started)

            transform = Transform(columns=columns, columnTransforms=columnTransforms)
            transform.validate()
            masking = _bindMasking(job, jobConfig, columns, log=False)
            jsonColumns = _decodedJsonColumns(masking, getattr(stream, 'description', None), getattr(settings, 'type', None), columns)
            prepare = _preparer(transform, masking, times=times, jsonColumns=jsonColumns)

            read = 0
            complete = True
            chunks = timedChunks(stream, times, readLimitFor(jobConfig.sourceConnection, settings))
            try:
                for index, chunk in enumerate(chunks):
                    prepare(index, chunk)
                    read += len(chunk)
                    if read >= rows:
                        # Whether the query had more is unknown without
                        # reading it, which is what stopping here saves.
                        complete = False
                        break
            finally:
                chunks.close()

    except Exception as error:
        return BenchResult(job=job, error=describeError(error))

    return BenchResult(job=job, rows=read, complete=complete, connectSeconds=round(connectSeconds, 3), stages=times.asDict(),
                       partitioned=jobConfig.partitions is not None)


def masker(threads: int) -> str:
    """Which masker measured, and with how many threads, for the report."""

    native = maskingModule.nativeVersion()
    if native is None:
        return 'Python, one thread, about ten times slower than the native masker: {}'.format(maskingModule.nativeUnavailableReason())

    return 'bauta-rs {}, {} thread(s)'.format(native, threads)


def _rate(value: Optional[float]) -> str:

    return '-' if value is None else '{:,.0f}'.format(value)


def renderBench(results: List[BenchResult], maskerLine: str) -> str:
    """The results as a table, with what each says about where to look."""

    lines = ['masker: {}'.format(maskerLine),
             'Reading and masking were measured in turn, and nothing was written. A run overlaps them, so whichever is slower sets its pace,',
             'unless writing to the target is slower still.',
             '',
             '{:<28} {:>10} {:>8} {:>12} {:>12} {:>7}  {}'.format('JOB', 'ROWS', 'CONNECT', 'READ ROWS/S', 'MASK ROWS/S', 'WAITED', 'SETS THE PACE')]

    for result in results:
        if result.error is not None:
            lines.append('{:<28} failed: {}'.format(result.job, result.error))
            continue
        stages = result.stages or {}
        lines.append('{:<28} {:>10} {:>8.2f} {:>12} {:>12} {:>7.1f}  {}'.format(
            result.job, '{:,}{}'.format(result.rows, '' if result.complete else '+'), result.connectSeconds,
            _rate(result.rate('read')), _rate(result.rate('mask')), stages.get('throttled', 0.0), result.pace or '-'))

    notes = []
    if any(result.rows and not result.complete for result in results):
        notes.append('+ the query returns more rows than were read; --rows reads more.')
    if any(result.partitioned for result in results):
        notes.append('Jobs with partitions were read as one stream; a run reads their slices at once.')
    if any((result.stages or {}).get('throttled') for result in results):
        notes.append('WAITED is seconds held back by the source\'s maxRowsReadPerSecond, and READ ROWS/S excludes it.')

    return '\n'.join(lines + ([''] + notes if notes else [])) + '\n'


def benchReport(results: List[BenchResult], maskerLine: str) -> Dict[str, Any]:
    """The results as JSON."""

    return {
        'masker': maskerLine,
        'jobs': [{
            'job': result.job,
            'rows': result.rows,
            'complete': result.complete,
            'connectSeconds': result.connectSeconds,
            **({'readSeconds': result.stages['read'], 'maskSeconds': result.stages['mask'], 'throttledSeconds': result.stages['throttled'],
                'readRowsPerSecond': result.rate('read'), 'maskRowsPerSecond': result.rate('mask'), 'pace': result.pace}
               if result.stages is not None else {}),
            'error': result.error,
            } for result in results],
        }
