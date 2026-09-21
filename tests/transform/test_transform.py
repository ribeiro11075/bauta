import pytest

from bauta.transform import Transform, TransformError, TransformResolutionError, resolveTransformer


def test_transform_applies_an_arbitrary_callable():
    rows = [(1, 100.5), (2, None)]

    transform = Transform(data=rows, columns=['id', 'amount'], columnTransforms={'amount': [lambda v: '${:,.2f}'.format(v or 0)]})

    assert transform.transform() == [(1, '$100.50'), (2, '$0.00')]


def test_transform_applies_multiple_transformers_in_order():
    rows = [(1,)]

    transform = Transform(data=rows, columns=['n'], columnTransforms={'n': [lambda v: v + 1, lambda v: v * 10]})

    assert transform.transform() == [(20,)]


def test_transform_with_no_data_returns_it_unchanged():
    transform = Transform(data=[], columns=['id'], columnTransforms={'id': [lambda v: v + 1]})

    assert transform.transform() == []


def test_transform_leaves_untransformed_columns_alone():
    rows = [(1, 'a')]

    transform = Transform(data=rows, columns=['id', 'name'], columnTransforms={})

    assert transform.transform() == [(1, 'a')]


def test_transform_raises_when_a_transform_names_a_column_not_in_columns():
    """A column not in `columns` -- e.g. left out of targetColumns, or never
    actually selected -- should fail loudly and up front, rather than the
    transform silently never running.
    """
    transform = Transform(data=[(1,)], columns=['id'], columnTransforms={'doesNotExist': [lambda v: v]})

    with pytest.raises(TransformError, match='doesNotExist'):
        transform.transform()


def test_transform_raises_when_a_transform_names_a_column_not_in_columns_even_with_no_rows():
    """The unknown-column check runs before the (data-empty) early return, so a
    misconfigured job fails the same way regardless of how much data it moves.
    """
    transform = Transform(data=[], columns=['id'], columnTransforms={'doesNotExist': [lambda v: v]})

    with pytest.raises(TransformError, match='doesNotExist'):
        transform.transform()


def test_transform_wraps_a_failing_transformer_with_column_and_type_context():
    def onlyAcceptsStrings(value):
        return value.upper()

    transform = Transform(data=[(1,)], columns=['id'], columnTransforms={'id': [onlyAcceptsStrings]})

    with pytest.raises(TransformError, match='column "id"') as excinfo:
        transform.transform()

    assert 'onlyAcceptsStrings' in str(excinfo.value)
    assert 'of type int' in str(excinfo.value)


def test_a_failing_transformer_never_reveals_the_value():
    """Transforms see raw production rows, before masking, and the error text
    reaches logs and the run result. The transformer's own message is dropped
    too, since it usually quotes the value.
    """
    def parseNumber(value):
        return int(value)

    transform = Transform(data=[('123-45-6789',)], columns=['ssn'], columnTransforms={'ssn': [parseNumber]})

    with pytest.raises(TransformError) as excinfo:
        transform.transform()

    assert '123-45-6789' not in str(excinfo.value)
    assert excinfo.value.__cause__ is None
    assert excinfo.value.__suppress_context__


def test_resolve_transformer_finds_a_real_function():
    """bauta itself ships no transforms module (that lives in example/, since
    it's reference content, not library code) -- resolve a stdlib function instead
    to test the resolution mechanics without depending on example/'s existence.
    """
    transformer = resolveTransformer('os.path:basename')

    assert transformer('/a/b/c.txt') == 'c.txt'


def test_resolve_transformer_requires_a_colon():
    with pytest.raises(TransformResolutionError):
        resolveTransformer('os.path.basename')


def test_resolve_transformer_raises_for_missing_module():
    with pytest.raises(TransformResolutionError):
        resolveTransformer('not_a_real_module_xyz:fn')


def test_resolve_transformer_raises_for_missing_attribute():
    with pytest.raises(TransformResolutionError):
        resolveTransformer('os.path:doesNotExist')


def test_resolve_transformer_raises_for_non_callable_attribute():
    with pytest.raises(TransformResolutionError):
        resolveTransformer('os.path:sep')


def test_a_reference_can_carry_literal_arguments():
    transformer = resolveTransformer("os.path:join('b', 'c')")

    assert transformer('a') == 'a/b/c'
    assert transformer.__name__ == "join('b', 'c')"


def test_arguments_may_be_keywords_and_collections():
    transformer = resolveTransformer("bauta.transform.builtinTransforms:regexReplace(pattern='[aeiou]', replacement='')")

    assert transformer('banana') == 'bnn'


@pytest.mark.parametrize('reference', [
    'os.path:basename(__import__("os").system("true"))',
    'os.path:basename(open)',
    "os.path:basename(*['x'])",
    'os.path:basename(**{})',
    'os.path:basename(1) + x(2',
    'os.path:basename(',
    ])
def test_arguments_that_are_not_plain_literals_are_refused(reference):
    """A configuration file must not be a way to run code."""
    with pytest.raises(TransformResolutionError):
        resolveTransformer(reference)


def test_arguments_that_do_not_fit_the_function_are_refused_up_front():
    with pytest.raises(TransformResolutionError, match='too many positional arguments'):
        resolveTransformer('bauta.transform.builtinTransforms:upper(1)')

    with pytest.raises(TransformResolutionError, match="unexpected keyword argument 'width'"):
        resolveTransformer('bauta.transform.builtinTransforms:truncate(width=3)')


def test_a_failing_transformer_with_arguments_is_named_with_them():
    transformer = resolveTransformer('bauta.transform.builtinTransforms:truncate(-1)')
    transform = Transform(data=[('abc',)], columns=['name'], columnTransforms={'name': [transformer]})

    with pytest.raises(TransformError, match=r'transformer "truncate\(-1\)" failed on column "name"'):
        transform.transform()


def test_apply_with_no_transforms_returns_the_rows_untouched():
    """Every chunk of every job goes through apply(), and most jobs configure
    no transforms: this used to copy each row to a list and back to a tuple to
    change nothing.
    """
    rows = [(1, 'a'), (2, 'b')]

    result = Transform(columns=['id', 'name'], columnTransforms={}).apply(rows)

    assert result == rows
    assert result[0] is rows[0]


def test_apply_transforms_one_column_and_leaves_the_others_as_they_were():
    rows = [(1, 'a', 'keep me'), (2, 'b', 'me too')]

    result = Transform(columns=['id', 'name', 'note'], columnTransforms={'name': [str.upper]}).apply(rows)

    assert result == [(1, 'A', 'keep me'), (2, 'B', 'me too')]
    # The untouched columns carry through as the very objects that were read.
    assert result[0][2] is rows[0][2]


def test_a_column_whose_transform_list_is_empty_costs_nothing():
    """An empty list for a column is the same as not naming it at all."""
    rows = [(1, 'a')]

    transform = Transform(columns=['id', 'name'], columnTransforms={'name': []})

    assert transform.apply(rows) == rows
    assert transform.apply(rows)[0] is rows[0]
