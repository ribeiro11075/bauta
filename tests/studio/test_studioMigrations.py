"""Studio's tables, upgraded in place from one release to the next."""
import pytest

pytest.importorskip('alembic', reason='bauta Studio needs its extra: pip install "bauta[studio]"')

import sqlalchemy as sa  # noqa: E402
from alembic.autogenerate import compare_metadata  # noqa: E402
from alembic.migration import MigrationContext  # noqa: E402
from alembic.script import ScriptDirectory  # noqa: E402
from sqlalchemy.orm import Session  # noqa: E402

from bauta.studio.db import BASELINE_REVISION, MIGRATIONS_DIRECTORY, VERSION_TABLE, Base, Database, User  # noqa: E402


def _url(tmp_path):
    return 'sqlite:///{}'.format(tmp_path / 'studio.db')


def _head():
    return ScriptDirectory(str(MIGRATIONS_DIRECTORY)).get_current_head()


def _revision(engine):
    with engine.connect() as connection:
        return connection.execute(sa.text('SELECT version_num FROM {}'.format(VERSION_TABLE))).scalar()


def test_a_new_database_is_migrated_to_the_latest_revision(tmp_path):
    database = Database(_url(tmp_path))

    assert _revision(database.engine) == _head()
    database.close()


def test_the_migrations_create_what_the_models_declare(tmp_path):
    """A model changed without a migration would work on a new database's
    create_all and fail on every upgraded one, so the two are compared."""

    database = Database(_url(tmp_path))
    with database.engine.connect() as connection:
        context = MigrationContext.configure(connection, opts={'version_table': VERSION_TABLE})
        differences = [difference for difference in compare_metadata(context, Base.metadata)]
    database.close()

    assert differences == []


def test_a_database_made_before_migrations_keeps_its_rows(tmp_path):
    """0.2.7 made its tables with create_all and recorded nothing; running the
    baseline over them would fail on the first table that exists."""

    engine = sa.create_engine(_url(tmp_path))
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        session.add(User(email='kept@example.com', passwordHash='x'))
        session.commit()
    engine.dispose()

    database = Database(_url(tmp_path))
    with database.open() as session:
        assert session.scalar(sa.select(User.email)) == 'kept@example.com'
    assert _head() >= BASELINE_REVISION and _revision(database.engine) == _head()
    database.close()


def test_opening_an_up_to_date_database_again_changes_nothing(tmp_path):
    Database(_url(tmp_path)).close()
    database = Database(_url(tmp_path))

    assert _revision(database.engine) == _head()
    database.close()
