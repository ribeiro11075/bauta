"""Keeps example/orchestrators/ from rotting.

Airflow and Dagster are too heavy to install for the suite, so the DAG and
Dagster files are loaded against stand-ins for the few names they use, which
record what the files build: the DAGs, tasks, dependencies and commands, and
the assets, jobs and schedules. The demo runs the same commands for real, a
process per job, and a Dagster asset is materialized against it.
"""
import importlib.util
import sys
import types
from pathlib import Path

import pytest

from tests.examples.demos import EXAMPLES, loadedDemo

ORCHESTRATORS = EXAMPLES / 'orchestrators'


@pytest.fixture
def demoRun(tmp_path, monkeypatch):
    monkeypatch.syspath_prepend(str(ORCHESTRATORS))
    with loadedDemo('orchestrators', 'orchestratorsDemo') as demo:
        yield demo.main(tmp_path / 'transaction')
    sys.modules.pop('bautaTasks', None)


def test_the_demo_runs_a_process_per_job_side_by_side_and_a_full_refresh_removes_a_deleted_row(demoRun):
    assert demoRun['graph'] == {'loadProducts': [], 'maskCustomers': [], 'maskOrders': ['maskCustomers', 'loadProducts']}
    # Every task succeeded: two `run --job` processes at once took no lock from each other.
    for stage in ('first', 'incremental', 'refresh'):
        assert {job: outcome[2] for job, outcome in demoRun[stage].items()} == {job: 0 for job in demoRun['graph']}
    assert demoRun['sideBySide']
    # maskOrders started only once both of its predecessors had finished.
    assert demoRun['first']['maskOrders'][0] >= max(demoRun['first'][job][1] for job in ('maskCustomers', 'loadProducts'))

    assert (3,) in demoRun['copied'] and (3,) in demoRun['afterIncremental']
    assert (3,) not in demoRun['afterRefresh'] and len(demoRun['afterRefresh']) == 9
    assert demoRun['checks'] == {'audit': 0, 'discover --update': 0}


def loadTasks(monkeypatch):
    monkeypatch.syspath_prepend(str(ORCHESTRATORS))
    sys.modules.pop('bautaTasks', None)
    import bautaTasks
    return bautaTasks


def test_the_graph_is_read_without_the_secrets_it_names_and_leaves_inactive_jobs_out(tmp_path, monkeypatch):
    monkeypatch.delenv('MASKING_KEY', raising=False)
    (tmp_path / 'jobs.d').mkdir()
    (tmp_path / 'jobs.yaml').write_text('workers: 1\ndefaults:\n  masking:\n    key: ${MASKING_KEY}\ninclude: [jobs.d/*.yaml]\njobs:\n'
                                        '  a: {}\n  b: {predecessors: [a, off]}\n  off: {active: false}\n')
    (tmp_path / 'jobs.d' / 'more.yaml').write_text('jobs:\n  c: {predecessors: [b]}\n')

    assert loadTasks(monkeypatch).jobGraph(tmp_path) == {'a': [], 'b': ['a'], 'c': ['b']}


def test_the_command_is_this_interpreters_bauta_unless_bauta_command_says_otherwise(monkeypatch):
    bautaTasks = loadTasks(monkeypatch)

    monkeypatch.delenv('BAUTA_COMMAND', raising=False)
    assert bautaTasks.command('run', configDirectory=Path('/etc/bauta'), job='j', fullRefresh=True) == [
        sys.executable, '-m', 'bauta', 'run', '--config', '/etc/bauta', '--quiet', '--job', 'j', '--full-refresh']

    monkeypatch.setenv('BAUTA_COMMAND', '/opt/bauta/bin/bauta')
    assert bautaTasks.command('validate', configDirectory=Path('/etc/bauta'))[:2] == ['/opt/bauta/bin/bauta', 'validate']


def loadFile(name, monkeypatch, stubs):
    """An example file, run against `stubs` in place of its imports."""
    for module, contents in stubs.items():
        monkeypatch.setitem(sys.modules, module, contents)
    monkeypatch.syspath_prepend(str(ORCHESTRATORS))
    sys.modules.pop('bautaTasks', None)
    specification = importlib.util.spec_from_file_location(name.replace('.', '_'), ORCHESTRATORS / name)
    module = importlib.util.module_from_spec(specification)
    specification.loader.exec_module(module)
    return module


def module(name, **contents):
    created = types.ModuleType(name)
    created.__dict__.update(contents)
    return created


@pytest.fixture
def airflow(monkeypatch):
    """Stand-ins for DAG and BashOperator that record what a DAG file builds."""

    class DAG:
        created = []
        current = None

        def __init__(self, dag_id, **settings):
            self.dag_id, self.settings, self.tasks = dag_id, settings, {}

        def __enter__(self):
            DAG.current = self
            DAG.created.append(self)
            return self

        def __exit__(self, *exception):
            DAG.current = None

    class BashOperator:
        def __init__(self, task_id, bash_command, **settings):
            self.task_id, self.bash_command, self.settings, self.upstream = task_id, bash_command, settings, set()
            DAG.current.tasks[task_id] = self

        def __rshift__(self, other):
            other.upstream.add(self.task_id)
            return other

    stubs = {'pendulum': module('pendulum', datetime=lambda *parts, **zone: parts),
             'airflow': module('airflow', DAG=DAG),
             'airflow.operators': module('airflow.operators'),
             'airflow.operators.bash': module('airflow.operators.bash', BashOperator=BashOperator)}
    return lambda name: {dag.dag_id: dag for dag in (loadFile(name, monkeypatch, stubs), DAG.created)[1]}


def test_the_airflow_dag_runs_bauta_as_one_task_and_refreshes_weekly_after_the_checks(airflow):
    dags = airflow('airflowDag.py')

    assert dags['bauta'].settings['schedule'] == '*/15 * * * *' and dags['bauta'].settings['max_active_runs'] == 1
    run = dags['bauta'].tasks['run']
    assert run.upstream == {'validate'} and run.settings['pool'] == 'bauta'
    assert ' -m bauta run --config ' in run.bash_command and '--job' not in run.bash_command

    weekly = dags['bauta_full_refresh'].tasks
    assert dags['bauta_full_refresh'].settings['schedule'] == '0 3 * * 0'
    assert (weekly['discover_update'].upstream, weekly['full_refresh'].upstream) == ({'audit'}, {'discover_update'})
    assert weekly['full_refresh'].bash_command.endswith('--full-refresh') and weekly['full_refresh'].settings['pool'] == 'bauta'
    assert run.settings['env'] == {'MASKING_KEY': '{{ var.value.bauta_masking_key }}'} and run.settings['append_env']


def test_the_per_job_airflow_dag_has_a_task_per_job_downstream_of_its_predecessors(airflow):
    dags = airflow('airflowDagPerJob.py')

    for dagId, fullRefresh in (('bauta_jobs', False), ('bauta_jobs_full_refresh', True)):
        tasks = dags[dagId].tasks
        assert {task: sorted(tasks[task].upstream) for task in tasks} == {
            'loadProducts': [], 'maskCustomers': [], 'maskOrders': ['loadProducts', 'maskCustomers']}
        assert tasks['maskOrders'].bash_command.endswith('--job maskOrders' + (' --full-refresh' if fullRefresh else ''))
        assert all(task.settings['pool'] == 'bauta_jobs' and task.settings['retries'] == 2 for task in tasks.values())


@pytest.fixture
def dagster(monkeypatch):
    """Stand-ins for the Dagster names the file uses, recording what it defines."""

    class Config:
        def __init__(self, **values):
            self.__dict__.update(values)

    class Failure(Exception):
        def __init__(self, description, metadata=None):
            super().__init__(description)
            self.description, self.metadata = description, metadata

    class Asset:
        def __init__(self, function, **settings):
            self.function, self.settings = function, settings

    def asset(**settings):
        return lambda function: Asset(function, **settings)

    stubs = {'dagster': module(
        'dagster', AssetExecutionContext=object, AssetsDefinition=Asset, Config=Config, Failure=Failure, asset=asset,
        AssetKey=lambda name: ('key', name), AssetSelection=types.SimpleNamespace(groups=lambda group: ('groups', group)),
        define_asset_job=lambda name, selection, config=None: {'name': name, 'selection': selection, 'config': config},
        ScheduleDefinition=lambda job, cron_schedule: {'job': job['name'], 'cron': cron_schedule},
        Definitions=lambda **definitions: definitions)}
    return lambda: loadFile('dagsterDefinitions.py', monkeypatch, stubs)


def test_dagster_has_an_asset_per_job_and_a_weekly_job_that_refreshes_each_in_full(dagster):
    definitions = dagster()
    assets = {asset.settings['name']: asset for asset in definitions.defs['assets']}

    assert {name: asset.settings['deps'] for name, asset in assets.items()} == {
        'loadProducts': [], 'maskCustomers': [], 'maskOrders': [('key', 'maskCustomers'), ('key', 'loadProducts')]}
    assert all(asset.settings['op_tags'] == {'dagster/concurrency_key': 'bauta'} for asset in assets.values())

    jobs = {job['name']: job for job in definitions.defs['jobs']}
    assert jobs['bauta']['config'] is None
    assert jobs['bauta_full_refresh']['config'] == {'ops': {name: {'config': {'fullRefresh': True}} for name in assets}}
    assert definitions.defs['schedules'] == [{'job': 'bauta', 'cron': '*/15 * * * *'}, {'job': 'bauta_full_refresh', 'cron': '0 3 * * 0'}]


def test_a_dagster_asset_runs_its_job_and_fails_with_bautas_error(dagster, demoRun, monkeypatch, tmp_path):
    """Against the demo's databases: loadProducts succeeds, and a missing
    MASKING_KEY fails maskCustomers with bauta's own one-line error.
    """
    definitions = dagster()
    assets = {asset.settings['name']: asset for asset in definitions.defs['assets']}
    logged = []
    context = types.SimpleNamespace(log=types.SimpleNamespace(info=logged.append))
    for variable, value in (('ORCHESTRATOR_DEMO_PROD_PATH', 'prod.db'), ('ORCHESTRATOR_DEMO_STAGING_PATH', 'staging.db')):
        monkeypatch.setenv(variable, str(tmp_path / 'transaction' / value))
    monkeypatch.setenv('ORCHESTRATOR_DEMO_STATE', str(tmp_path / 'transaction'))
    monkeypatch.setenv('PYTHONPATH', str(ORCHESTRATORS.parents[1]))
    monkeypatch.setenv('MASKING_KEY', 'orchestrator-demo-masking-key-not-for-real-use')

    assets['loadProducts'].function(context, definitions.BautaRun())
    assert '--job loadProducts' in logged[0]

    monkeypatch.delenv('MASKING_KEY', raising=False)
    with pytest.raises(definitions.Failure, match=r'bauta exited 2: \[ERROR\] .*MASKING_KEY'):
        assets['maskCustomers'].function(context, definitions.BautaRun(fullRefresh=True))
