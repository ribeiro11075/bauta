"""A JSON column -- PostgreSQL's json and jsonb, MySQL's and DuckDB's JSON,
all arriving as their text -- masked by a policy that needs its value: the
value decoded before transforms and masking, and the result encoded as JSON
after. The end-to-end tests against each database are in tests/integration.
"""
import pytest

from bauta.configuration import DatabaseType
from bauta.database.values import jsonColumnIndexes
from bauta.jobs.pipeline import _decodedJsonColumns, _preparer
from bauta.masking import MaskingError, MaskingPlan
from bauta.transform import Transform

KEY = 'a-json-columns-unit-test-key-0123456'


def _plan(columns):
    return MaskingPlan(key=KEY, columns=columns).bind(list(columns))


@pytest.mark.parametrize('databaseType,description,expected', [
    (DatabaseType.POSTGRESQL, [('id', 23), ('j', 3802), ('k', 114), ('t', 25)], [1, 2]),
    (DatabaseType.MYSQL, [('id', 3), ('j', 245), ('t', 252)], [1]),
    (DatabaseType.DUCKDB, [('id', 'INTEGER'), ('j', 'JSON'), ('t', 'VARCHAR')], [1]),
    # MariaDB's JSON is LONGTEXT, which nothing tells from text.
    (DatabaseType.MARIADB, [('id', 3), ('j', 252)], []),
    (DatabaseType.SQLITE, [('id', None), ('j', None)], []),
    ])
def test_json_columns_are_known_by_what_each_driver_reports(databaseType, description, expected):
    assert jsonColumnIndexes(databaseType, description) == expected


def test_only_policies_that_need_the_value_have_it_decoded():
    """keep and json take JSON text as it is, and null drops it; everything
    else masks the value the text holds."""
    columns = {'kept': 'keep', 'fields': {'strategy': 'json', 'fields': {'a': 'hash'}}, 'dropped': 'null', 'address': 'email', 'fixed': {
        'strategy': 'constant', 'value': 'x'}}
    description = [(name, 3802) for name in columns]

    chosen = _decodedJsonColumns(_plan(columns), description, DatabaseType.POSTGRESQL, list(columns))

    assert chosen == [(3, 'address'), (4, 'fixed')]


def test_a_value_masks_as_the_value_it_holds_and_comes_back_as_json():
    import json

    columns = {'address': {'strategy': 'email', 'domain': 'email'}, 'plain': {'strategy': 'email', 'domain': 'email'}, 'fixed': {
        'strategy': 'constant', 'value': 'x'}, 'n': {'strategy': 'key', 'domain': 'n'}}
    masking = _plan(columns)
    jsonColumns = [(0, 'address'), (2, 'fixed'), (3, 'n')]
    prepare = _preparer(Transform(columns=list(columns), columnTransforms={}), masking, jsonColumns=jsonColumns)

    [(address, plain, fixed, number), (nullAddress, _, nullFixed, nullNumber)] = prepare(0, [
        ('"ana@corp.example"', 'ana@corp.example', '"anything"', '4815162342'), ('null', None, None, None)])

    assert json.loads(address) == plain and fixed == '"x"' and json.loads(number) == _plan({'n': {'strategy': 'key', 'domain': 'n'}}).apply(
        [(4815162342,)])[0][0]
    # JSON null and SQL NULL both mask to NULL, which every target takes.
    assert nullAddress is None and nullFixed == '"x"' and nullNumber is None


def test_text_that_is_not_json_in_a_json_column_fails_naming_the_column():
    masking = _plan({'address': 'email'})
    prepare = _preparer(Transform(columns=['address'], columnTransforms={}), masking, jsonColumns=[(0, 'address')])

    with pytest.raises(MaskingError, match='address is a JSON column, but holds text that is not JSON'):
        prepare(0, [('ana@corp.example',)])


def test_a_mask_json_cannot_hold_fails_naming_the_column_not_the_value(monkeypatch):
    masking = _plan({'score': 'hash'})
    monkeypatch.setattr(masking, 'apply', lambda rows, chunkIndex=0: [(float('nan'),) for _ in rows])
    prepare = _preparer(Transform(columns=['score'], columnTransforms={}), masking, jsonColumns=[(0, 'score')])

    with pytest.raises(MaskingError, match='score is a JSON column, and hash masked a value in it to a number JSON cannot hold'):
        prepare(0, [('1.5',)])


def test_json_lines_refuses_a_json_column_value_that_is_not_json():
    """Written raw, it made every line invalid while the job succeeded."""
    from bauta.lake.columns import FileTypeError
    from bauta.lake.formats import _rawJson

    encode = _rawJson('address')

    assert encode('"u3050fb9483cd@example.test"') == '"u3050fb9483cd@example.test"' and encode('5') == '5'
    with pytest.raises(FileTypeError, match='address is a JSON column, but a value in it is not JSON'):
        encode('u3050fb9483cd@example.test')
