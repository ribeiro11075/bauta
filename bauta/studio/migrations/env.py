"""Run by Alembic for each upgrade, on the connection db.migrate gives it."""
from alembic import context

from bauta.studio.db import VERSION_TABLE, Base

context.configure(connection=context.config.attributes['connection'], target_metadata=Base.metadata, version_table=VERSION_TABLE,
                  # SQLite alters a table by rebuilding it, which batch does.
                  render_as_batch=True)

with context.begin_transaction():
    context.run_migrations()
