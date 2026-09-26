import functools

import pytest

from bauta.configuration import (
    Configuration,
    ConfigurationError,
    connectionConfig,
    DataJobsFile,
    findCycle,
    )
from tests.jobConfigs import dataJobFields


def test_database_configuration_round_trips():
    raw = {
        'sourceDb': {'type': 'mysql', 'user': 'u', 'password': 'p', 'database': 'd', 'host': 'h', 'port': 3306},
        'targetDb': {'type': 'postgresql', 'user': 'u', 'password': 'p', 'database': 'd', 'host': 'h', 'port': 5432},
        }

    result = Configuration.validateConnectionConfiguration(raw)

    assert set(result.keys()) == {'sourceDb', 'targetDb'}
    assert (type(result['sourceDb']).__name__, type(result['targetDb']).__name__) == ('MySQLConnection', 'PostgreSQLConnection')


def test_database_configuration_rejects_unknown_type():
    with pytest.raises(ConfigurationError):
        Configuration.validateConnectionConfiguration({'db': {'type': 'mongodb', 'user': 'u', 'password': 'p', 'database': 'd', 'host': 'h'}})


def test_sqlite_connection_needs_only_a_path():
    assert connectionConfig(type='sqlite', path='/tmp/some.db').describeTarget() == 'sqlite file /tmp/some.db'


@pytest.mark.parametrize('raw, message', [
    ({'type': 'postgresql', 'database': 'd', 'host': 'h', 'user': 'u', 'password': 'p', 'serviceName': 'orcl'},
     'serviceName is a setting of oracle connections, not postgresql'),
    ({'type': 'sqlite', 'path': 'x.db', 'host': 'h', 'port': 5432},
     'host is a setting of mariadb, mssql, mysql, oracle and postgresql connections, not sqlite'),
    ({'type': 'sqlite', 'database': 'x.db'}, 'a sqlite connection names its file with path'),
    ({'type': 'oracle', 'database': 'd', 'host': 'h', 'user': 'u', 'password': 'p', 'serviceName': 's'},
     'database is a setting of mariadb, mssql, mysql and postgresql connections, not oracle'),
    ({'type': 'mysql', 'database': 'd', 'host': 'h', 'user': 'u', 'password': 'p', 'currentSchema': 'x'},
     'currentSchema is a setting of duckdb, oracle and postgresql connections, not mysql'),
    ])
def test_a_setting_of_another_connection_type_is_refused_naming_the_types_it_belongs_to(raw, message):
    """One model took every type's settings, so an Oracle serviceName on a
    PostgreSQL connection, or a host and password on a SQLite one, were
    accepted and ignored -- against the rule that a setting nobody reads is
    an error.
    """
    with pytest.raises(ConfigurationError, match=message):
        Configuration.validateConnectionConfiguration({'db': raw})


@pytest.mark.parametrize('missingField', ['user', 'password', 'host'])
def test_non_sqlite_connections_require_network_credentials(missingField):
    kwargs = dict(type='mysql', user='u', password='p', database='d', host='h')
    kwargs[missingField] = None

    with pytest.raises(ConfigurationError):
        connectionConfig(**kwargs)


@pytest.mark.parametrize('serviceName,sid,shouldRaise', [
    ('svc', None, False),
    (None, 'sid1', False),
    ('svc', 'sid1', True),
    (None, None, True),
    ])
def test_oracle_requires_exactly_one_of_service_name_or_sid(serviceName, sid, shouldRaise):
    kwargs = dict(type='oracle', user='u', password='p', host='h', serviceName=serviceName, sid=sid)

    if shouldRaise:
        with pytest.raises(ConfigurationError):
            connectionConfig(**kwargs)
    else:
        connectionConfig(**kwargs)


def test_swap_strategy_requires_target_table_stage():
    raw = {
        'workers': 1,
        'jobs': {
            'job1': {
                'active': True, 'sourceConnection': 'a', 'targetConnection': 'b', 'insertStrategy': 'swap',
                'chunkSize': 100, 'targetTableFinal': 't', 'sourceQuery': 'select 1',
                },
            },
        }

    with pytest.raises(ConfigurationError):
        Configuration.validateJobConfiguration(raw, DataJobsFile)


def test_upsert_strategy_does_not_require_target_table_stage():
    raw = {
        'workers': 1,
        'jobs': {
            'job1': {
                'active': True, 'sourceConnection': 'a', 'targetConnection': 'b', 'insertStrategy': 'upsert',
                'chunkSize': 100, 'targetTableFinal': 't', 'sourceQuery': 'select 1',
                },
            },
        }

    jobsFile = Configuration.validateJobConfiguration(raw, DataJobsFile)
    assert jobsFile.jobs['job1'].targetTableStage is None


def test_validate_job_graph_catches_unknown_predecessor():
    raw = {'workers': 1, 'jobs': {'job1': {'active': True, 'predecessors': ['doesNotExist'], 'sourceConnection': 'a',
                                            'targetConnection': 'b', 'insertStrategy': 'upsert', 'chunkSize': 1,
                                            'targetTableFinal': 't', 'sourceQuery': 'select 1'}}}
    jobsFile = Configuration.validateJobConfiguration(raw, DataJobsFile)

    with pytest.raises(ConfigurationError, match='doesNotExist'):
        Configuration.validateJobGraph(jobsFile.jobs)


@pytest.mark.parametrize('setting', ['sourceConnection', 'targetConnection'])
def test_validate_job_graph_catches_unknown_database_alias(setting):
    jobsFile = Configuration.validateJobConfiguration({'workers': 1, 'jobs': {'job1': _job(**{setting: 'ghost'})}}, DataJobsFile)

    with pytest.raises(ConfigurationError, match='job1: {} "ghost" is not a known connection alias'.format(setting)):
        Configuration.validateJobGraph(jobsFile.jobs, connectionAliases={'a'})


@pytest.mark.parametrize('setting', ['retries', 'retryDelaySeconds'])
def test_retries_cannot_be_negative(setting):
    with pytest.raises(ConfigurationError, match='{} cannot be negative'.format(setting)):
        Configuration.validateJobConfiguration({'workers': 1, 'jobs': {'job1': _job(**{setting: -1})}}, DataJobsFile)


def test_validate_job_graph_passes_for_valid_config():
    raw = {'workers': 1, 'jobs': {'job1': {'active': True, 'sourceConnection': 'a', 'targetConnection': 'b',
                                            'insertStrategy': 'upsert', 'chunkSize': 1, 'targetTableFinal': 't',
                                            'sourceQuery': 'select 1'}}}
    jobsFile = Configuration.validateJobConfiguration(raw, DataJobsFile)

    Configuration.validateJobGraph(jobsFile.jobs, connectionAliases={'a', 'b'})


@pytest.mark.parametrize('field,rawValue', [
    ('predecessors', [None]),
    ('preTargetAdhocQueries', None),
    ])
def test_yaml_null_list_idiom_is_treated_as_empty(field, rawValue):
    """YAML's "key:\\n-\\n" idiom parses to [None]; a bare omitted key parses to
    None. Both should become an empty list rather than a validation error.
    """
    raw = {'workers': 1, 'jobs': {'job1': {
        'active': True, 'sourceConnection': 'a', 'targetConnection': 'b', 'insertStrategy': 'upsert', 'chunkSize': 1,
        'targetTableFinal': 't', 'sourceQuery': 'select 1', field: rawValue,
        }}}

    jobsFile = Configuration.validateJobConfiguration(raw, DataJobsFile)

    assert getattr(jobsFile.jobs['job1'], field) == []


def test_cycle_sleep_seconds_defaults_and_is_configurable():
    raw = {'workers': 1, 'jobs': {}}
    jobsFile = Configuration.validateJobConfiguration(raw, DataJobsFile)
    assert jobsFile.cycleSleepSeconds == 0.5

    raw = {'workers': 1, 'cycleSleepSeconds': 5, 'jobs': {}}
    jobsFile = Configuration.validateJobConfiguration(raw, DataJobsFile)
    assert jobsFile.cycleSleepSeconds == 5


_job = functools.partial(dataJobFields, sourceConnection='a', sourceQuery='select 1', targetConnection='a', targetTableFinal='t', chunkSize=10)


def test_a_predecessor_cycle_is_rejected_rather_than_run_forever():
    """Every job in a cycle waits for another that waits for it, so a run
    containing one could never finish. It has to fail validation instead.
    """
    raw = {'workers': 1, 'jobs': {'first': _job(predecessors=['second']), 'second': _job(predecessors=['first']), 'third': _job()}}
    jobsFile = Configuration.validateJobConfiguration(raw, DataJobsFile)

    with pytest.raises(ConfigurationError, match='cycle.*first -> second -> first'):
        Configuration.validateJobGraph(jobsFile.jobs, connectionAliases={'a'})


def test_a_job_that_is_its_own_predecessor_is_a_cycle():
    raw = {'workers': 1, 'jobs': {'only': _job(predecessors=['only'])}}
    jobsFile = Configuration.validateJobConfiguration(raw, DataJobsFile)

    with pytest.raises(ConfigurationError, match='only -> only'):
        Configuration.validateJobGraph(jobsFile.jobs)


def test_find_cycle_accepts_a_diamond_and_ignores_unknown_predecessors():
    assert findCycle({'a': [], 'b': ['a'], 'c': ['a'], 'd': ['b', 'c', 'missing']}) is None


def test_workers_must_be_positive():
    with pytest.raises(ConfigurationError, match='workers'):
        Configuration.validateJobConfiguration({'workers': 0, 'jobs': {}}, DataJobsFile)


def test_a_swap_stage_table_must_share_the_targets_schema():
    """A rename never moves a table between schemas, so the three-way rename
    would fail part-way.
    """
    raw = {'workers': 1, 'jobs': {'j': _job(insertStrategy='swap', targetTableFinal='sales.orders', targetTableStage='staging.orders')}}

    with pytest.raises(ConfigurationError, match='same schema'):
        Configuration.validateJobConfiguration(raw, DataJobsFile)


@pytest.mark.parametrize('final,stage', [('orders', 'orders_stage'), ('sales.orders', 'SALES.orders_stage'),
                                        ('"sales"."group"', 'sales."group_stage"')])
def test_a_swap_stage_table_in_the_same_schema_is_accepted(final, stage):
    raw = {'workers': 1, 'jobs': {'j': _job(insertStrategy='swap', targetTableFinal=final, targetTableStage=stage)}}

    Configuration.validateJobConfiguration(raw, DataJobsFile)


def test_a_password_never_appears_in_the_models_repr():
    settings = connectionConfig(type='postgresql', user='u', password='hunter2', database='d', host='h')

    assert 'hunter2' not in repr(settings)
    assert 'hunter2' not in str(settings.model_dump())
    assert settings.plainPassword() == 'hunter2'


def _watermarkJob(**overrides):
    fields = dict(active=True, sourceConnection='src', targetConnection='tgt', insertStrategy='upsert', chunkSize=100,
                  targetTableFinal='orders', sourceQuery='select id, updated_at from orders where updated_at > {{ watermark }}',
                  watermarkColumn='updated_at', watermarkInitial='1970-01-01')
    fields.update(overrides)
    return {'workers': 1, 'jobs': {'loadOrders': fields}}


def test_a_watermark_job_validates():
    jobsFile = Configuration.validateJobConfiguration(_watermarkJob(), DataJobsFile)

    assert jobsFile.jobs['loadOrders'].watermarkColumn == 'updated_at'


def test_watermark_column_without_a_placeholder_is_rejected():
    """The column and the token are two halves of one feature: a column with no
    token extracts everything, then advances a watermark nothing filtered on.
    """
    raw = _watermarkJob(sourceQuery='select id, updated_at from orders')

    with pytest.raises(ConfigurationError, match='no {{ watermark }} placeholder'):
        Configuration.validateJobConfiguration(raw, DataJobsFile)


def test_a_placeholder_without_a_watermark_column_is_rejected():
    raw = _watermarkJob(watermarkColumn=None)

    with pytest.raises(ConfigurationError, match='watermarkColumn is not set'):
        Configuration.validateJobConfiguration(raw, DataJobsFile)


def test_watermark_without_an_initial_value_is_rejected():
    raw = _watermarkJob(watermarkInitial=None)

    with pytest.raises(ConfigurationError, match='watermarkInitial is required'):
        Configuration.validateJobConfiguration(raw, DataJobsFile)


def test_watermark_with_swap_is_rejected():
    """The important one: swap replaces the target with the stage contents, so an
    incremental extract would stage only changed rows and delete everything else.
    """
    raw = _watermarkJob(insertStrategy='swap', targetTableStage='orders_stage')

    with pytest.raises(ConfigurationError, match='requires insertStrategy: upsert'):
        Configuration.validateJobConfiguration(raw, DataJobsFile)


@pytest.mark.parametrize('placeholder', ['{{ watermark }}', '{{watermark}}', '{{   watermark   }}'])
def test_placeholder_spacing_is_forgiving(placeholder):
    raw = _watermarkJob(sourceQuery='select id, updated_at from orders where updated_at > {}'.format(placeholder))

    Configuration.validateJobConfiguration(raw, DataJobsFile)


_SERVER = dict(user='u', password='p', host='h')
_CONNECTIONS = {'postgresql': dict(_SERVER, database='d'), 'oracle': dict(_SERVER, serviceName='s'), 'duckdb': dict(path='x.duckdb'),
                'mysql': dict(_SERVER, database='d'), 'mariadb': dict(_SERVER, database='d'), 'mssql': dict(_SERVER, database='d'),
                'sqlite': dict(path='x.db')}


@pytest.mark.parametrize('databaseType', ['postgresql', 'oracle', 'duckdb'])
def test_current_schema_is_accepted_where_a_session_can_switch_schema(databaseType):
    settings = connectionConfig(type=databaseType, currentSchema='reporting', **_CONNECTIONS[databaseType])

    assert settings.currentSchema == 'reporting'


@pytest.mark.parametrize('databaseType', ['mysql', 'mariadb', 'mssql', 'sqlite'])
def test_current_schema_is_refused_where_it_cannot_be_set(databaseType):
    with pytest.raises(ConfigurationError, match='currentSchema is a setting of duckdb, oracle and postgresql connections, not {}'.format(databaseType)):
        Configuration.validateConnectionConfiguration({'db': dict(type=databaseType, currentSchema='reporting', **_CONNECTIONS[databaseType])})


def test_current_schema_must_be_a_plain_identifier():
    """It is written into a session statement, so nothing but a name gets through."""
    with pytest.raises(ConfigurationError, match='plain identifier'):
        Configuration.validateConnectionConfiguration({'db': {'type': 'postgresql', 'user': 'u', 'password': 'p', 'database': 'd', 'host': 'h',
                                                            'currentSchema': 'x; drop table t'}})


def test_connection_options_are_kept_out_of_the_models_repr():
    settings = connectionConfig(type='oracle', user='u', password='p', host='h', serviceName='s',
                                options={'wallet_password': 'hunter2', 'protocol': 'tcps', 'ignored': None})

    assert settings.options == {'wallet_password': 'hunter2', 'protocol': 'tcps'}
    assert 'hunter2' not in repr(settings)


@pytest.mark.parametrize('timeout', [0, -5])
def test_timeout_seconds_must_be_positive(timeout):
    with pytest.raises(ConfigurationError, match='timeoutSeconds'):
        Configuration.validateJobConfiguration({'workers': 1, 'jobs': {'j': _job(timeoutSeconds=timeout)}}, DataJobsFile)


def _validateJob(**overrides):
    return Configuration.validateJobConfiguration({'workers': 1, 'jobs': {'job': _job(**overrides)}}, DataJobsFile)


@pytest.mark.parametrize('chunkSize', [0, -1])
def test_a_chunk_size_below_one_is_rejected(chunkSize):
    """A chunk of zero rows read nothing, and a swap then replaced the target with it."""
    with pytest.raises(ConfigurationError, match='chunkSize'):
        _validateJob(chunkSize=chunkSize, insertStrategy='swap', targetTableStage='t_stage')


@pytest.mark.parametrize('strategy', ['upsert', 'swap'])
@pytest.mark.parametrize('stage', ['t', 'T', '"t"', '[t]'])
def test_a_stage_table_that_is_the_target_is_rejected(strategy, stage):
    """The stage table is emptied first, so it would empty the target."""
    with pytest.raises(ConfigurationError, match='different table from targetTableFinal'):
        _validateJob(insertStrategy=strategy, targetTableStage=stage)


@pytest.mark.parametrize('command', ['', '   ', 'token "unterminated', [], ['  ']])
def test_a_password_command_that_names_no_program_is_rejected(command):
    raw = {'warehouse': {'type': 'postgresql', 'database': 'w', 'host': 'h', 'user': 'u', 'passwordCommand': command}}

    with pytest.raises(ConfigurationError, match='passwordCommand'):
        Configuration.validateConnectionConfiguration(raw)


def test_running_a_malformed_password_command_is_a_configuration_error():
    from bauta.configuration import runPasswordCommand

    for command in ('  ', 'echo "x'):
        with pytest.raises(ConfigurationError, match='passwordCommand'):
            runPasswordCommand(command)


MASKING_KEY = 'a-configuration-test-masking-key'


def _jobsFile(job, **fileLevel):
    settings = {'workers': 1, 'jobs': {'copyCustomers': job}}
    settings.update(fileLevel)
    return settings


def test_a_misspelled_masking_block_is_rejected_rather_than_ignored():
    """The reason unknown keys are forbidden: `maskng` was silently dropped, so
    a job that looked masked copied every column as it stood.
    """
    raw = _jobsFile(_job(maskng={'key': MASKING_KEY, 'columns': {'id': 'keep', 'email': 'email'}}))

    with pytest.raises(ConfigurationError) as error:
        Configuration.validateJobConfiguration(raw, DataJobsFile)

    assert 'maskng' in str(error.value)


def test_a_misspelled_setting_is_rejected_at_every_level():
    for raw in (_jobsFile(_job(chunksize=500)),
                _jobsFile(_job(masking={'key': MASKING_KEY, 'columns': {'id': 'keep'}, 'defaultStrategyy': 'keep'})),
                _jobsFile(_job(), workerss=1)):
        with pytest.raises(ConfigurationError):
            Configuration.validateJobConfiguration(raw, DataJobsFile)


def test_a_misspelled_connection_setting_is_rejected():
    with pytest.raises(ConfigurationError):
        Configuration.validateConnectionConfiguration({'db': {'type': 'sqlite', 'path': 'd.db', 'hostt': 'h'}})


def test_top_level_anchor_keys_are_left_for_yaml_to_use():
    """`x-` keys hold anchors the jobs merge from, as docker-compose uses them,
    and must survive the rule that every other unknown key is an error.
    """
    raw = _jobsFile(_job(), **{'x-defaults': {'chunkSize': 500}})

    assert Configuration.validateJobConfiguration(raw, DataJobsFile).workers == 1

    databases = Configuration.validateConnectionConfiguration(
        {'x-shared': {'type': 'sqlite'}, 'db': {'type': 'sqlite', 'path': 'd.db'}})

    assert set(databases) == {'db'}


def test_defaults_supply_what_a_job_does_not_name():
    raw = _jobsFile({'sourceQuery': 'select id, email from customers', 'targetTableFinal': 'customers',
                     'masking': {'columns': {'id': 'keep', 'email': 'email'}}},
                    defaults={'active': True, 'sourceConnection': 'prod', 'targetConnection': 'staging',
                              'insertStrategy': 'upsert', 'chunkSize': 500, 'masking': {'key': MASKING_KEY}})

    job = Configuration.validateJobConfiguration(raw, DataJobsFile).jobs['copyCustomers']

    assert (job.sourceConnection, job.targetConnection, job.chunkSize) == ('prod', 'staging', 500)
    assert job.masking is not None and job.masking.key.get_secret_value() == MASKING_KEY


def test_a_job_keeps_its_own_value_over_a_default():
    raw = _jobsFile(_job(chunkSize=10), defaults={'chunkSize': 500, 'sourceConnection': 'elsewhere'})

    job = Configuration.validateJobConfiguration(raw, DataJobsFile).jobs['copyCustomers']

    assert job.chunkSize == 10 and job.sourceConnection == 'a'


def test_defaults_do_not_give_an_unmasked_job_a_masking_block():
    """An unmasked job must stay visibly unmasked, rather than becoming a key
    with no policy.
    """
    raw = _jobsFile(_job(), defaults={'masking': {'key': MASKING_KEY}})

    assert Configuration.validateJobConfiguration(raw, DataJobsFile).jobs['copyCustomers'].masking is None


def test_defaults_refuse_settings_that_belong_to_one_job():
    for defaults in ({'sourceQuery': 'select 1'}, {'targetTableFinal': 'customers'},
                     {'masking': {'columns': {'id': 'keep'}}}):
        with pytest.raises(ConfigurationError):
            Configuration.validateJobConfiguration(_jobsFile(_job(), defaults=defaults), DataJobsFile)


def test_unmasked_cannot_be_declared_beside_a_masking_policy():
    raw = _jobsFile(_job(unmasked=True, masking={'key': MASKING_KEY, 'columns': {'id': 'keep', 'email': 'email'}}))

    with pytest.raises(ConfigurationError) as error:
        Configuration.validateJobConfiguration(raw, DataJobsFile)

    assert 'unmasked' in str(error.value)


def _databases(**overrides):
    settings = {'prod': {'type': 'sqlite', 'path': 'prod.db'}, 'staging': {'type': 'sqlite', 'path': 'staging.db'}}
    for alias, extra in overrides.items():
        settings[alias] = {**settings[alias], **extra}
    return Configuration.validateConnectionConfiguration(settings)


def test_a_database_that_requires_masking_refuses_an_unmasked_job():
    jobsFile = Configuration.validateJobConfiguration(_jobsFile(_job(sourceConnection='prod', targetConnection='staging')), DataJobsFile)

    for requiring in ({'prod': {'requireMasking': True}}, {'staging': {'requireMasking': True}}):
        with pytest.raises(ConfigurationError) as error:
            Configuration.validateJobGraph(jobsFile.jobs, connections=_databases(**requiring))

        assert 'requireMasking' in str(error.value)


def test_requiring_masking_is_not_waived_by_a_job_declaring_itself_unmasked():
    """`unmasked` records a decision about one job; requireMasking is the
    database's, and nothing overrides it.
    """
    raw = _jobsFile(_job(sourceConnection='prod', targetConnection='staging', unmasked=True))
    jobsFile = Configuration.validateJobConfiguration(raw, DataJobsFile)

    with pytest.raises(ConfigurationError):
        Configuration.validateJobGraph(jobsFile.jobs, connections=_databases(staging={'requireMasking': True}))


def test_a_masked_job_satisfies_a_database_that_requires_masking():
    raw = _jobsFile(_job(sourceConnection='prod', targetConnection='staging',
                         masking={'key': MASKING_KEY, 'columns': {'id': 'keep'}}))
    jobsFile = Configuration.validateJobConfiguration(raw, DataJobsFile)

    Configuration.validateJobGraph(jobsFile.jobs, connections=_databases(staging={'requireMasking': True}))


def test_a_job_needs_only_what_is_particular_to_it():
    """active, chunkSize and workers have defaults, so a minimal file says only
    what this job does that another wouldn't.
    """
    raw = {'jobs': {'copyCustomers': {'sourceConnection': 'prod', 'sourceQuery': 'select id from customers',
                                      'targetConnection': 'staging', 'targetTableFinal': 'customers',
                                      'insertStrategy': 'upsert'}}}

    jobsFile = Configuration.validateJobConfiguration(raw, DataJobsFile)
    job = jobsFile.jobs['copyCustomers']

    assert jobsFile.workers == 1
    assert job.active is True and job.chunkSize == 5000


def test_insert_strategy_stays_required():
    """The one job setting where a wrong value gives wrong data rather than an
    error: upsert never removes rows deleted in production, swap replaces the
    table wholesale. It has to be chosen, not defaulted.
    """
    raw = {'jobs': {'copyCustomers': {'sourceConnection': 'prod', 'sourceQuery': 'select id from customers',
                                      'targetConnection': 'staging', 'targetTableFinal': 'customers'}}}

    with pytest.raises(ConfigurationError) as error:
        Configuration.validateJobConfiguration(raw, DataJobsFile)

    assert 'insertStrategy' in str(error.value)


def test_a_connection_describes_what_it_points_at_without_the_password():
    sqlite, postgres = Configuration.validateConnectionConfiguration({
        'lite': {'type': 'sqlite', 'path': 'copy.db'},
        'warehouse': {'type': 'postgresql', 'database': 'analytics', 'host': 'db.example', 'port': 5432,
                      'user': 'etl', 'password': 'hunter2'},
        }).values()

    assert sqlite.describeTarget().startswith('sqlite file /') and sqlite.describeTarget().endswith('copy.db')
    assert postgres.describeTarget() == 'postgresql analytics on db.example:5432'
    assert 'hunter2' not in postgres.describeTarget()



def test_duckdb_runs_one_job_at_a_time_and_other_connections_as_many_as_configured():
    connections = Configuration.validateConnectionConfiguration({
        'lake': {'type': 'duckdb', 'path': 'lake.duckdb'},
        'prod': {'type': 'sqlite', 'path': 'prod.db', 'maxConcurrentJobs': 3},
        'staging': {'type': 'sqlite', 'path': 'staging.db'},
        })

    assert {alias: settings.jobLimit() for alias, settings in connections.items()} == {'lake': 1, 'prod': 3, 'staging': None}


def test_duckdb_refuses_more_than_one_job_at_a_time():
    with pytest.raises(ConfigurationError, match='maxConcurrentJobs cannot be above 1 for duckdb'):
        Configuration.validateConnectionConfiguration({'lake': {'type': 'duckdb', 'path': 'lake.duckdb', 'maxConcurrentJobs': 2}})


def test_two_aliases_for_one_duckdb_file_are_refused():
    """Each alias would count its jobs apart, so two jobs could open the file
    at once, one of them waiting a minute and failing.
    """
    with pytest.raises(ConfigurationError, match='give each file one alias: a, b all name'):
        Configuration.validateConnectionConfiguration({'a': {'type': 'duckdb', 'path': 'lake.duckdb'},
                                                     'b': {'type': 'duckdb', 'path': './lake.duckdb'}})


def test_a_duckdb_connection_needs_no_host_or_login_and_names_its_file_when_it_fails():
    settings = connectionConfig(type='duckdb', path='lake.duckdb')

    assert settings.describeTarget().endswith('lake.duckdb') and settings.describeTarget().startswith('duckdb file /')


def test_an_alias_reaching_a_duckdb_file_through_a_symlink_is_the_same_file(tmp_path):
    (tmp_path / 'lake.duckdb').touch()
    (tmp_path / 'link.duckdb').symlink_to(tmp_path / 'lake.duckdb')

    with pytest.raises(ConfigurationError, match='give each file one alias'):
        Configuration.validateConnectionConfiguration({'a': {'type': 'duckdb', 'path': str(tmp_path / 'lake.duckdb')},
                                                     'b': {'type': 'duckdb', 'path': str(tmp_path / 'link.duckdb')}})
