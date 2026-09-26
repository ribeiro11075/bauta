"""A data job with every required setting filled in, for the tests that care
about one or two of them. Each suite fixes the defaults its tests read --
the aliases, the table -- with functools.partial, and a test overrides the
rest by keyword.
"""
from typing import Any, Dict

from bauta.configuration import DataJobConfig


def dataJobFields(**overrides: Any) -> Dict[str, Any]:
    """The job as it would be written in jobs.yaml."""

    fields: Dict[str, Any] = dict(active=True, sourceConnection='source', sourceQuery='select * from customers', targetConnection='target',
                                  targetTableFinal='customers', insertStrategy='upsert', chunkSize=100)
    fields.update(overrides)

    return fields


def dataJob(**overrides: Any) -> DataJobConfig:
    """The job validated, as the runner and the reports take it."""

    return DataJobConfig(**dataJobFields(**overrides))
