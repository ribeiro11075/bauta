"""Everything that differs between the six databases: one module each, the
DatabaseDialect contract they share in `base`, and how a name is read and
quoted in `names`.
"""
from .base import ColumnCategory, ColumnDefinition, DatabaseDialect, ForeignKey, durationText
from .mssql import MSSQLDialect
from .mysql import MariaDBDialect, MySQLDialect
from .names import (IDENTIFIER_LIMITS, bareName, catalogName, catalogTable, catalogTableName, quoteFolded, quoteFoldedTable, quoteIdentifier,
                    quoteTableName, splitTableName, suffixedName, tooLongName, unqualifiedName)
from .oracle import OracleDialect
from .postgresql import PostgreSQLDialect
from .sqlite import SQLiteDialect

__all__ = [
    'bareName',
    'catalogName',
    'catalogTable',
    'catalogTableName',
    'ColumnCategory',
    'ColumnDefinition',
    'DatabaseDialect',
    'durationText',
    'ForeignKey',
    'IDENTIFIER_LIMITS',
    'MariaDBDialect',
    'MSSQLDialect',
    'MySQLDialect',
    'OracleDialect',
    'PostgreSQLDialect',
    'quoteFolded',
    'quoteFoldedTable',
    'quoteIdentifier',
    'quoteTableName',
    'splitTableName',
    'SQLiteDialect',
    'suffixedName',
    'tooLongName',
    'unqualifiedName',
    ]
