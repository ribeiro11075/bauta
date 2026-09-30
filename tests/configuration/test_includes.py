"""A jobs file split across files with `include`: merged before validation,
so defaults and every check reach the included jobs, and nothing defined
twice is silently replaced.
"""
import pytest

from bauta.configuration import Configuration, ConfigurationError, DataJobsFile, readJobsFile

JOB = {'sourceQuery': 'SELECT id FROM t', 'targetTableFinal': 't', 'unmasked': True}


def write(path, text):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)
    return path


@pytest.fixture
def jobsFile(tmp_path):
    return write(tmp_path / 'jobs.yaml', '''workers: 1
defaults:
  sourceConnection: prod
  targetConnection: copy
  insertStrategy: upsert
include:
- jobs.d/*.yaml
jobs:
  loadMain: {sourceQuery: SELECT id FROM main, targetTableFinal: main, unmasked: true}
''')


def test_included_jobs_are_merged_and_take_the_jobs_files_defaults(jobsFile):
    write(jobsFile.parent / 'jobs.d' / 'sales.yaml', 'jobs:\n  loadOrders: {sourceQuery: SELECT id FROM orders, targetTableFinal: orders, unmasked: true}\n')
    write(jobsFile.parent / 'jobs.d' / 'billing.yaml', 'jobs:\n  loadInvoices: {sourceQuery: SELECT id FROM invoices, targetTableFinal: invoices, unmasked: true}\n')

    document = readJobsFile(jobsFile)
    jobs = Configuration.validateJobConfiguration(document.content, DataJobsFile).jobs

    assert set(jobs) == {'loadMain', 'loadOrders', 'loadInvoices'}
    assert {job.sourceConnection for job in jobs.values()} == {'prod'}
    # The jobs file first, then each pattern's matches in sorted order.
    assert [path.name for path in document.files] == ['jobs.yaml', 'billing.yaml', 'sales.yaml']
    assert document.origins['loadOrders'].name == 'sales.yaml'
    assert document.origins['loadMain'] == jobsFile


def test_a_job_defined_in_two_files_names_both(jobsFile):
    write(jobsFile.parent / 'jobs.d' / 'a.yaml', 'jobs:\n  loadMain: {sourceQuery: SELECT 1, targetTableFinal: x, unmasked: true}\n')

    with pytest.raises(ConfigurationError, match=r'job loadMain is defined in both .*jobs\.yaml and .*a\.yaml'):
        readJobsFile(jobsFile)


def test_a_pattern_matching_nothing_is_an_error_not_an_empty_set_of_jobs(jobsFile):
    with pytest.raises(ConfigurationError, match=r'include jobs\.d/\*\.yaml matches no file'):
        readJobsFile(jobsFile)


@pytest.mark.parametrize('setting', ['workers: 2', 'defaults: {chunkSize: 10}', 'include: [more.yaml]', 'memory: m.yaml'])
def test_an_included_file_holds_only_jobs_and_acknowledged(jobsFile, setting):
    write(jobsFile.parent / 'jobs.d' / 'a.yaml', setting + '\njobs: {}\n')

    with pytest.raises(ConfigurationError, match='an included file holds jobs: and acknowledged: alone'):
        readJobsFile(jobsFile)


def test_acknowledged_tables_merge_and_a_table_acknowledged_twice_is_an_error(jobsFile):
    write(jobsFile.parent / 'jobs.d' / 'a.yaml', 'acknowledged:\n  prod:\n    audit_log: never leaves production\n')
    write(jobsFile.parent / 'jobs.d' / 'b.yaml', 'acknowledged:\n  prod:\n    payroll: HR data\n')

    assert readJobsFile(jobsFile).content['acknowledged'] == {'prod': {'audit_log': 'never leaves production', 'payroll': 'HR data'}}

    write(jobsFile.parent / 'jobs.d' / 'c.yaml', 'acknowledged:\n  prod:\n    payroll: again\n')
    with pytest.raises(ConfigurationError, match=r'table payroll in prod is acknowledged in both .*b\.yaml and .*c\.yaml'):
        readJobsFile(jobsFile)


def test_a_jobs_file_with_only_includes_needs_no_jobs_of_its_own(tmp_path):
    jobsFile = write(tmp_path / 'jobs.yaml', 'workers: 1\ninclude: [team/*.yaml]\n')
    write(tmp_path / 'team' / 'a.yaml', 'jobs:\n  loadA: {sourceConnection: p, targetConnection: c, insertStrategy: upsert, '
                                         'sourceQuery: SELECT 1, targetTableFinal: a, unmasked: true}\n')

    assert set(Configuration.validateJobConfiguration(readJobsFile(tmp_path / 'jobs.yaml').content, DataJobsFile).jobs) == {'loadA'}
    assert jobsFile.exists()


def test_include_is_refused_when_validated_from_a_mapping_that_never_resolved_it():
    with pytest.raises(ConfigurationError, match='readJobsFile'):
        Configuration.validateJobConfiguration({'workers': 1, 'include': ['a.yaml'], 'jobs': {}}, DataJobsFile)
