"""What `bauta coverage` says about a source database's tables.

The question audit cannot answer: a table with no job has nothing to audit.
"""
import functools

from bauta.review.coverage import ACKNOWLEDGED, COPIED, MASKED, UNCOVERED, coverageReport, renderCoverage
from tests.jobConfigs import dataJob

KEY = 'a-coverage-test-masking-key'


_job = functools.partial(dataJob, sourceDatabase='prod', sourceQuery='select id from customers', targetDatabase='staging')


def _states(report):
    return {entry['table']: entry['state'] for entry in report['tables']}


def test_a_table_no_job_reads_is_not_covered():
    report = coverageReport('prod', ['customers', 'audit_log'], {'maskCustomers': _job()})

    assert _states(report) == {'customers': COPIED, 'audit_log': UNCOVERED}
    assert report['summary'][UNCOVERED] == 1


def test_a_masked_job_and_an_unmasked_one_are_told_apart():
    jobs = {
        'maskCustomers': _job(masking={'key': KEY, 'columns': {'id': 'keep'}}),
        'copyCountries': _job(sourceQuery='select id from countries', targetTableFinal='countries'),
        }

    assert _states(coverageReport('prod', ['customers', 'countries'], jobs)) == {'customers': MASKED, 'countries': COPIED}


def test_a_job_reading_another_database_does_not_cover_the_table():
    """The alias matters: a table of the same name elsewhere is not this one."""
    report = coverageReport('prod', ['customers'], {'fromWarehouse': _job(sourceDatabase='warehouse')})

    assert _states(report) == {'customers': UNCOVERED}


def test_an_acknowledged_table_is_covered_and_carries_its_reason():
    report = coverageReport('prod', ['audit_log'], {}, acknowledged={'audit_log': 'never leaves production'})

    (entry,) = report['tables']
    assert entry['state'] == ACKNOWLEDGED and entry['reason'] == 'never leaves production'
    assert report['summary'][UNCOVERED] == 0


def test_an_acknowledgement_for_a_table_that_is_gone_is_reported_as_stale():
    report = coverageReport('prod', ['customers'], {'maskCustomers': _job()},
                            acknowledged={'was_dropped': 'no longer exists'})

    assert report['acknowledgedButAbsent'] == ['WAS_DROPPED']


def test_an_uncovered_table_says_which_columns_look_like_personal_data():
    report = coverageReport('prod', ['audit_log'], {}, columns={'audit_log': ['id', 'actor_email', 'action']})

    (entry,) = report['tables']
    assert entry['personalDataColumns'] == ['actor_email']


def test_columns_of_a_covered_table_are_not_reported():
    """Only what nothing covers is worth reading columns for."""
    report = coverageReport('prod', ['customers'], {'maskCustomers': _job()}, columns={'customers': ['email']})

    assert 'personalDataColumns' not in report['tables'][0]


def test_a_table_matches_its_job_however_the_name_is_written():
    """A reserved word is quoted in a job and bare in a catalog."""
    jobs = {'loadOrder': _job(sourceQuery='select id from "order"', targetTableFinal='orders')}

    assert _states(coverageReport('prod', ['order'], jobs)) == {'order': COPIED}


def test_the_report_puts_what_is_wrong_first():
    jobs = {'maskCustomers': _job(masking={'key': KEY, 'columns': {'id': 'keep'}})}
    report = coverageReport('prod', ['customers', 'audit_log'], jobs, acknowledged={'employees': 'HR'})

    assert [entry['table'] for entry in report['tables']] == ['audit_log', 'customers']


def test_the_text_report_names_every_state():
    jobs = {'maskCustomers': _job(masking={'key': KEY, 'columns': {'id': 'keep'}}),
            'copyCountries': _job(sourceQuery='select id from countries', targetTableFinal='countries')}
    text = renderCoverage(coverageReport('prod', ['customers', 'countries', 'audit_log'], jobs,
                                         acknowledged={'audit_log': 'never copied'}))

    assert 'copied and masked by maskCustomers' in text
    assert 'copied as it stands by copyCountries' in text
    assert 'not copied, declared -- never copied' in text
    assert 'NOT COVERED: 0.' in text
