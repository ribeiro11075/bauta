"""`partitions: auto` and `count: auto`: how many slices a job chooses, and
what it reads as one stream instead. Slicing on a real server is in
tests/integration/test_integration_databases.py.
"""
import logging
import sqlite3

import pytest

from bauta.configuration import Configuration, ConfigurationError, connectionConfig, DatabaseType, DataJobsFile, partitionLimitProblems
from bauta.jobs import pipeline
from bauta.jobs.dependencyGraph import DependencyGraph
from bauta.jobs.partitions import MINIMUM_ROWS_PER_SLICE, CoreBudget, automaticCount, maskingThreadsWith
from tests.jobConfigs import dataJob, dataJobFields

KEY = 'an-automatic-partitions-test-key'


def _budget(share=4, threads=1, automatic=False, places=None):
    return CoreBudget(share=share, maskingThreads=threads, automaticThreads=automatic, places=places)


# --- how many slices ------------------------------------------------------------------------

def test_the_count_is_the_fewest_the_cores_the_connections_and_the_rows_allow():
    large = 100 * MINIMUM_ROWS_PER_SLICE

    assert automaticCount(large, _budget(share=5), False)[0] == 5
    assert automaticCount(large, _budget(share=5, places=3), False) == (3, 'set by the places its connections have free (3)')
    assert automaticCount(3 * MINIMUM_ROWS_PER_SLICE, _budget(share=8), False) == (3, 'set by its size (one slice per 250,000 rows)')
    assert automaticCount(MINIMUM_ROWS_PER_SLICE, _budget(share=8), False)[0] == 1


def test_a_job_masking_in_python_is_one_stream():
    """Measured: four slices masking in Python ran 1.02 times as fast as one."""
    count, why = automaticCount(10 ** 9, _budget(share=8), True)

    assert count == 1 and 'interpreter lock' in why


def test_slices_take_over_automatic_masking_threads_and_leave_a_number_alone():
    # auto gave the job 4 threads: 4 slices each mask on their own thread instead.
    assert maskingThreadsWith(4, _budget(threads=4, automatic=True)) == 1
    # Fewer slices than threads: the pool masks for all of them, faster.
    assert maskingThreadsWith(2, _budget(threads=4, automatic=True)) == 4
    # A number is what the configuration asked for.
    assert maskingThreadsWith(8, _budget(threads=3, automatic=False)) == 3
    assert maskingThreadsWith(1, _budget(threads=4, automatic=True)) == 4


# --- configuration --------------------------------------------------------------------------

def test_auto_is_a_shorthand_for_the_column_and_the_count():
    automatic = dataJob(partitions='auto').partitions
    counted = dataJob(partitions={'column': 'id', 'count': 'auto'}).partitions
    fixed = dataJob(partitions={'column': 'id', 'count': 4}).partitions

    assert (automatic.column, automatic.automatic) == (None, True)
    assert (counted.column, counted.automatic) == ('id', True)
    assert (fixed.column, fixed.automatic) == ('id', False)


@pytest.mark.parametrize('partitions', [{'count': 4}, {'column': 'id', 'count': 1}, 'all', {'column': 'id', 'count': 'many'}])
def test_partitions_name_a_column_and_a_count_or_are_auto(partitions):
    with pytest.raises(Exception):
        dataJob(partitions=partitions)


def test_auto_can_be_every_job_s_default_and_is_ignored_for_files():
    raw = {'workers': 1, 'defaults': {'partitions': 'auto'},
           'jobs': {'toTable': dataJobFields(), 'toFiles': dataJobFields(insertStrategy='append', targetConnection='lake')}}

    jobsFile = Configuration.validateJobConfiguration(raw, DataJobsFile)

    assert all(job.partitions.automatic for job in jobsFile.jobs.values())
    with pytest.raises(ConfigurationError, match='partitions is for a table in a database'):
        Configuration.validateJobConfiguration({'workers': 1, 'jobs': {'toFiles': dataJobFields(
            insertStrategy='append', targetConnection='lake', partitions={'column': 'id', 'count': 4})}}, DataJobsFile)


def test_an_automatic_count_is_never_refused_for_a_limited_connection():
    connections = {'source': connectionConfig(type=DatabaseType.DUCKDB, path='/tmp/x.duckdb'),
                   'target': connectionConfig(type=DatabaseType.SQLITE, path='/tmp/x.db')}

    assert partitionLimitProblems(dataJob(partitions='auto'), connections) == []
    assert partitionLimitProblems(dataJob(partitions={'column': 'id', 'count': 4}), connections)


# --- places on limited connections ---------------------------------------------------------------

def test_an_automatic_job_holds_one_place_to_start_and_what_it_is_given_after():
    jobs = {'auto': dataJob(partitions='auto', sourceConnection='pg', targetConnection='pg'),
            'other': dataJob(sourceConnection='pg', targetConnection='pg')}
    graph = DependencyGraph(jobs, connectionLimits={'pg': 4})

    assert set(graph.takeReady()) == {'auto', 'other'}
    assert graph.places('auto') == 1 and graph.freePlaces('auto') == 3

    graph.reserve('auto', 3)

    assert graph.places('auto') == 3 and graph.freePlaces('other') == 1
    assert DependencyGraph({'free': dataJob(partitions='auto')}).freePlaces('free') is None


# --- read as one stream -----------------------------------------------------------------------

@pytest.fixture
def sqliteFiles(tmp_path):
    for name in ('source', 'target'):
        connection = sqlite3.connect(tmp_path / '{}.db'.format(name))
        connection.execute('CREATE TABLE customers (id INTEGER PRIMARY KEY, name TEXT)')
        if name == 'source':
            connection.executemany('INSERT INTO customers VALUES (?, ?)', [(index, 'name{}'.format(index)) for index in range(100)])
        connection.commit()
        connection.close()

    return {name: connectionConfig(type=DatabaseType.SQLITE, path=str(tmp_path / '{}.db'.format(name))) for name in ('source', 'target')}


def _messages(monkeypatch):
    messages = []
    handler = logging.Handler()
    handler.emit = lambda record: messages.append(record.getMessage())
    logger = logging.getLogger('bauta')
    logger.addHandler(handler)
    monkeypatch.setattr(logger, 'level', logging.INFO)

    return messages, lambda: logger.removeHandler(handler)


def test_a_job_writing_sqlite_is_one_stream_and_loads_every_row(sqliteFiles, monkeypatch):
    messages, stop = _messages(monkeypatch)
    try:
        outcome = pipeline._executeDataJob('j', dataJob(sourceQuery='SELECT id, name FROM customers', partitions='auto',
                                                        masking={'key': KEY, 'columns': {'id': 'keep', 'name': 'hash'}}), sqliteFiles)
    finally:
        stop()

    assert outcome.rowCount == 100
    assert any('as one stream: SQLite commits one write at a time' in message for message in messages)


@pytest.mark.parametrize('targetKey,why', [(None, 'its target has no primary key'), ('id, name', 'primary key is 2 columns'),
                                           ('code', 'does not return its target\'s primary key, code')])
def test_a_target_without_a_one_column_key_the_query_returns_is_one_stream(sqliteFiles, monkeypatch, targetKey, why):
    """Asked of the chooser directly: a job writing SQLite never gets as far."""
    connection = sqlite3.connect(sqliteFiles['target'].path)
    connection.execute('DROP TABLE customers')
    definition = 'id INTEGER, name TEXT, code INTEGER' + (', PRIMARY KEY ({})'.format(targetKey) if targetKey else '')
    connection.execute('CREATE TABLE customers ({})'.format(definition))
    connection.commit()
    connection.close()
    settings = dict(sqliteFiles, target=connectionConfig(type=DatabaseType.POSTGRESQL, host='h', user='u', password='p', database='d'))

    from bauta.database import Database
    from bauta.jobs.targets import TableTarget

    messages, stop = _messages(monkeypatch)
    job = dataJob(sourceQuery='SELECT id, name FROM customers', partitions='auto')
    try:
        with Database(connectionSettings=sqliteFiles['source']) as source, Database(connectionSettings=sqliteFiles['target']) as target:
            predicates = pipeline._automaticPredicates('j', job, settings, source, TableTarget(target, job), job.sourceQuery, None,
                                                       ['id', 'name'], None)
    finally:
        stop()

    assert predicates == []
    assert any(why in message for message in messages), messages


def test_a_named_column_that_cannot_be_sliced_fails_as_with_a_count(sqliteFiles):
    with pytest.raises(ConfigurationError, match='"code" is not among the columns'):
        pipeline._executeDataJob('j', dataJob(sourceQuery='SELECT id, name FROM customers', partitions={'column': 'code', 'count': 'auto'},
                                              targetConnection='target'), dict(sqliteFiles))


def test_a_run_gives_each_automatic_job_a_budget_and_its_places(monkeypatch):
    from bauta.jobs import runner

    monkeypatch.setattr(runner, 'coreShare', lambda alongside: 6 // alongside)
    jobs = {'auto': dataJob(partitions='auto', sourceConnection='pg', targetConnection='pg'),
            'plain': dataJob(sourceConnection='pg', targetConnection='pg')}
    graph = DependencyGraph(jobs, connectionLimits={'pg': 3})
    graph.takeReady()

    budget = runner._coreBudget(graph, 'auto', jobs['auto'], 'auto', 3, 2)

    assert budget == CoreBudget(share=3, maskingThreads=3, automaticThreads=True, places=2)
    assert graph.places('auto') == 2
    assert runner._coreBudget(graph, 'plain', jobs['plain'], 2, 2, 2).places is None


def test_a_whole_run_with_auto_under_defaults_loads_every_row(sqliteFiles, tmp_path):
    from bauta.jobs.memory import FileMemory
    from bauta.jobs.runner import runDataJobs

    raw = {'workers': 2, 'maskingThreads': 'auto', 'defaults': {'partitions': 'auto'},
           'jobs': {'copy': dataJobFields(sourceQuery='SELECT id, name FROM customers', masking={'key': KEY, 'columns': {'id': 'keep', 'name': 'hash'}})}}

    result = runDataJobs(jobsFile=Configuration.validateJobConfiguration(raw, DataJobsFile), connectionConfiguration=sqliteFiles,
                         memory=FileMemory(tmp_path / 'memory.yaml'))

    assert result.succeeded and result.rowCount == 100
