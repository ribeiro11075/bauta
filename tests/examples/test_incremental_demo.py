"""Keeps example/incremental/demo.py from rotting.

The sample YAML config is validated by test_shipped_example_configuration.py;
this applies the same standard to the other half of example/. A showcase script
that silently breaks on a refactor is worse than no showcase -- you find out
when you run it in front of someone.
"""
import sqlite3

import pytest

from tests.examples.demos import loadedDemo


@pytest.fixture(scope='module')
def demo():
    """The demo sets DEMO_DB_PATH itself -- it's how its database.yaml finds
    the file -- so it is put back afterwards."""
    with loadedDemo('incremental', 'incremental_demo', ['DEMO_DB_PATH']) as module:
        yield module


@pytest.fixture(scope='module')
def demoRun(demo, tmp_path_factory):
    """Runs the demo once into a temporary directory, not the source tree."""
    workingDirectory = tmp_path_factory.mktemp('incremental_demo')

    return demo.main(workingDirectory=workingDirectory), workingDirectory


def test_the_demo_runs_end_to_end(demoRun):
    watermarks, _ = demoRun

    assert len(watermarks) == 3


def test_the_watermark_advances_then_holds(demoRun):
    """Exactly what the script claims to show: the first run takes everything,
    the second takes only what is new, the third finds nothing and leaves the
    stored watermark where it is.
    """
    watermarks, _ = demoRun

    assert watermarks == ['2026-01-03T00:00:00', '2026-01-04T00:00:00', '2026-01-04T00:00:00']


def test_the_edited_row_is_not_re_extracted(demoRun):
    """The discriminator the demo is built around. Row 1's name changes in the
    source between runs without its updatedAt moving, so a full re-extract would
    pick the change up and an incremental one cannot. Row counts alone prove
    nothing here, since upsert is idempotent.
    """
    _, workingDirectory = demoRun

    connection = sqlite3.connect(str(workingDirectory / 'demo.db'))
    try:
        rows = dict(connection.execute('SELECT id, name FROM ordersTarget').fetchall())
    finally:
        connection.close()

    assert rows[1] == 'first'
    assert rows[4] == 'fourth'
    assert len(rows) == 4


def test_the_demo_loads_its_job_from_the_shipped_configuration(demo):
    """The point of moving it out of an inline dict: the demo exercises the same
    YAML-plus-environment path a real deployment does.
    """
    jobs = demo.loadConfiguration('jobs.yaml')

    assert jobs['jobs']['loadOrders']['watermarkColumn'] == 'updatedAt'


def test_the_demo_writes_only_inside_the_directory_it_is_given(demoRun):
    """It defaults to its transaction/ folder, so a caller that supplies a directory must
    get everything there -- otherwise running the tests litters the source tree.
    """
    _, workingDirectory = demoRun

    assert {path.name for path in workingDirectory.iterdir()} == {'demo.db', 'memory.yaml', 'memory.yaml.lock', 'demo.log'}
