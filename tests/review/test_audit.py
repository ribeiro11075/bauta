"""The audit report: what it shows for each job, and what it flags."""
import functools
from typing import Any

from bauta.review.audit import auditJobs, renderAudit
from bauta.configuration import DataJobConfig
from tests.jobConfigs import dataJob

KEY = 'an-audit-test-masking-key'


_job = functools.partial(dataJob, sourceConnection='prod', targetConnection='staging')


def _masked(columns, **overrides: Any) -> DataJobConfig:
    masking = {'key': KEY, 'columns': columns}
    if 'defaultStrategy' in overrides:
        masking['defaultStrategy'] = overrides.pop('defaultStrategy')
    return _job(masking=masking, **overrides)


def _messages(report, severity=None):
    return [(finding['job'], finding['message']) for finding in report['findings'] if severity in (None, finding['severity'])]


def test_a_clean_policy_has_no_findings():
    report = auditJobs({'maskCustomers': _masked({'id': 'keep', 'email': 'email', 'notes': 'null'})})

    assert report['findings'] == []
    assert report['summary'] == {'error': 0, 'warning': 0, 'info': 0}
    (job,) = report['jobs']
    assert job['masked'] and not job['columnsResolved']
    assert [(column['column'], column['strategy']) for column in job['columns']] == [('id', 'keep'), ('email', 'email'), ('notes', 'null')]


def test_a_kept_column_whose_name_suggests_personal_data_is_flagged():
    report = auditJobs({'maskCustomers': _masked({'id': 'keep', 'phone_number': 'keep'})})

    assert _messages(report, 'warning') == [('maskCustomers', 'column phone_number is kept unmasked, but its name suggests a phone number')]


def test_a_default_strategy_of_keep_is_flagged():
    report = auditJobs({'maskCustomers': _masked({'email': 'email'}, defaultStrategy='keep')})

    assert ('maskCustomers', 'defaultStrategy is keep, so any column added to the source later is copied unmasked') in _messages(report, 'warning')


def test_resolved_columns_show_what_falls_to_the_default_strategy():
    report = auditJobs({'maskCustomers': _masked({'email': 'email'}, defaultStrategy='null')},
                       returnedColumns={'maskCustomers': ['EMAIL', 'notes', 'ssn']})

    (job,) = report['jobs']
    assert job['columnsResolved']
    assert [(column['column'], column['strategy'], column['source']) for column in job['columns']] == [
        ('EMAIL', 'email', 'column'), ('notes', 'null', 'defaultStrategy'), ('ssn', 'null', 'defaultStrategy')]
    assert _messages(report, 'info') == [('maskCustomers', '2 column(s) fall to defaultStrategy null: notes, ssn')]


def test_a_policy_that_does_not_cover_the_query_is_an_error():
    report = auditJobs({'maskCustomers': _masked({'email': 'email'})}, returnedColumns={'maskCustomers': ['email', 'ssn']})

    assert report['summary']['error'] == 1
    assert 'not in the masking policy: ssn' in report['findings'][0]['message']


def test_an_unmasked_copy_from_a_source_other_jobs_mask_is_flagged():
    report = auditJobs({
        'maskCustomers': _masked({'email': 'email'}),
        'copyOrders': _job(sourceQuery='select * from orders', targetTableFinal='orders'),
        'rollUp': _job(sourceConnection='staging', targetConnection='staging', targetTableFinal='summary'),
        })

    assert _messages(report, 'warning') == [
        ('copyOrders', 'copies from prod without masking, though other jobs mask what they read from it'),
        ('rollUp', 'copies from staging to staging without masking, so every column it returns is written as it stands. '
                   'Add a masking policy, or declare the choice with `unmasked: true`')]


def test_an_unencrypted_source_connection_is_flagged_for_masked_jobs():
    report = auditJobs({'maskCustomers': _masked({'email': 'email'})}, encryption={'prod': False, 'staging': True})

    assert _messages(report, 'warning') == [('maskCustomers', 'reads unmasked data from prod over a connection that is not encrypted')]
    assert report['connections'] == {'prod': {'encrypted': False}, 'staging': {'encrypted': True}}


def test_shuffle_on_an_incremental_job_is_flagged():
    job = _masked({'id': 'keep', 'updatedAt': 'keep', 'gender': 'shuffle'}, watermarkColumn='updatedAt', watermarkInitial='1970-01-01',
                  sourceQuery='select * from customers where updatedAt > {{ watermark }}')

    report = auditJobs({'incremental': job})

    assert any('shuffle on an incremental job' in message for _, message in _messages(report, 'warning'))


def test_findings_are_ordered_errors_first():
    report = auditJobs({'maskCustomers': _masked({'email': 'keep'}, defaultStrategy='null')},
                       returnedColumns={'maskCustomers': ['email', 'notes']}, unreachable={'other': 'boom'})

    assert [finding['severity'] for finding in report['findings']] == ['warning', 'info']


def test_the_text_report_names_every_column_and_finding():
    text = renderAudit(auditJobs({
        'maskCustomers': _masked({'email': 'keep'}, defaultStrategy='null'),
        'copyOrders': _job(active=False),
        }, encryption={'prod': None}))

    assert 'maskCustomers: prod -> staging.customers' in text
    assert 'email                        keep' in text
    assert 'any other column             null (defaultStrategy)' in text
    assert 'copyOrders (inactive): prod -> staging.customers\n  not masked' in text
    assert 'prod                         encryption unknown' in text
    assert 'WARNING  maskCustomers: column email is kept unmasked, but its name suggests an email address' in text


def test_fpe_without_strict_is_noted():
    report = auditJobs({'maskCustomers': _masked({'id': 'fpe', 'ssn': {'strategy': 'fpe', 'strict': True}})})

    assert _messages(report, 'info') == [('maskCustomers', 'fpe without strict on id: values too short for FF1 are masked with key instead')]


def _customersAndOrders(customerId, orderCustomerId, orderKey=KEY, orderTarget='staging'):
    return {
        'maskCustomers': _masked({'id': customerId, 'email': 'email'}, sourceQuery='select id, email from customers'),
        'maskOrders': _job(sourceQuery='select id, customer_id from orders', targetTableFinal='orders', targetConnection=orderTarget,
                           predecessors=['maskCustomers'],
                           masking={'key': orderKey, 'columns': {'id': 'keep', 'customer_id': orderCustomerId}}),
        }


def _crossJob(report):
    return [message for job, message in _messages(report, 'warning') if job is None]


def test_a_domain_masked_one_way_under_one_key_has_no_findings():
    customer = {'strategy': 'key', 'domain': 'customer'}

    assert auditJobs(_customersAndOrders(customer, customer))['findings'] == []


def test_a_domain_masked_two_ways_is_flagged():
    report = auditJobs(_customersAndOrders({'strategy': 'key', 'domain': 'customer'}, {'strategy': 'hash', 'domain': 'customer'}))

    assert _crossJob(report) == [
        'in staging, domain customer is masked 2 different ways, so its masks cannot match across them: '
        'hash for maskOrders.customer_id; key for maskCustomers.id. '
        'Mask the domain one way, or give columns that should not match a domain of their own']


def test_a_domain_masked_with_different_options_is_flagged():
    report = auditJobs(_customersAndOrders({'strategy': 'hash', 'domain': 'customer'}, {'strategy': 'hash', 'domain': 'customer', 'length': 20}))

    (message,) = _crossJob(report)
    assert 'hash for maskCustomers.id; hash (length: 20) for maskOrders.customer_id' in message


def test_a_domain_masked_under_two_keys_is_flagged():
    customer = {'strategy': 'key', 'domain': 'customer'}
    report = auditJobs(_customersAndOrders(customer, customer, orderKey='another-audit-masking-key'))

    (message,) = _crossJob(report)
    assert message.startswith('in staging, domain customer is masked under 2 different keys')
    assert 'maskCustomers.id' in message and 'maskOrders.customer_id' in message


def test_copies_in_different_target_databases_may_use_different_keys():
    customer = {'strategy': 'key', 'domain': 'customer'}

    assert _crossJob(auditJobs(_customersAndOrders(customer, customer, orderKey='another-audit-masking-key', orderTarget='vendor'))) == []


def test_default_domains_are_compared_too():
    report = auditJobs({
        'maskCustomers': _masked({'name': 'fakeName'}),
        'maskCompanies': _masked({'name': 'fakeCompany'}, targetTableFinal='companies'),
        })

    (message,) = _crossJob(report)
    assert 'domain name is masked 2 different ways' in message


def _foreignKey():
    from bauta.database.dialects import ForeignKey

    return ForeignKey('orders', ('customer_id',), 'customers', ('id',), 'fk_orders_customers')


def _connected(jobs, **overrides):
    arguments = dict(
        returnedColumns={'maskCustomers': ['id', 'email'], 'maskOrders': ['id', 'customer_id']},
        targetColumns={'maskCustomers': ['id', 'email'], 'maskOrders': ['id', 'customer_id']},
        foreignKeys={'staging': [_foreignKey()]})
    arguments.update(overrides)
    return auditJobs(jobs, **arguments)


def test_a_reference_masked_like_its_key_has_no_findings():
    customer = {'strategy': 'key', 'domain': 'customer'}

    assert _connected(_customersAndOrders(customer, customer))['findings'] == []


def test_a_reference_left_in_its_default_domain_is_flagged():
    report = _connected(_customersAndOrders('key', 'key'))
    fingerprint = report['jobs'][0]['keyFingerprint']

    assert _messages(report, 'warning') == [(
        'maskOrders',
        'in staging, orders.customer_id is masked with key in domain customer_id under key {0}, but customers.id, which it references, '
        'is masked with key in domain id under key {0} (by maskCustomers), so the copied references will not match'.format(fingerprint))]


def test_a_reference_copied_as_it_is_to_a_masked_key_is_flagged():
    jobs = _customersAndOrders('key', 'keep')

    (message,) = [message for _, message in _messages(_connected(jobs), 'warning')]
    assert message.startswith('in staging, orders.customer_id is not masked, but customers.id, which it references, is masked with key')


def _references(report):
    return [message for _, message in _messages(report, 'warning') if 'which it references' in message]


def test_a_job_without_masking_counts_as_copying_every_column_as_it_is():
    jobs = _customersAndOrders('keep', 'keep')
    jobs['maskOrders'] = _job(sourceQuery='select id, customer_id from orders', targetTableFinal='orders')

    assert _references(_connected(jobs)) == []

    jobs['maskCustomers'] = _masked({'id': 'key', 'email': 'email'}, sourceQuery='select id, email from customers')
    (message,) = _references(_connected(jobs))
    assert 'orders.customer_id is not masked' in message


def test_a_nulled_reference_cannot_break():
    assert _connected(_customersAndOrders('key', 'null'))['findings'] == []


def test_target_columns_are_matched_to_the_query_by_position():
    customer = {'strategy': 'key', 'domain': 'customer'}
    jobs = _customersAndOrders(customer, customer)
    jobs['maskOrders'] = _job(sourceQuery='select id, owner from orders', targetTableFinal='app.ORDERS',
                              masking={'key': KEY, 'columns': {'id': 'keep', 'owner': {'strategy': 'hash', 'domain': 'customer'}}})

    report = _connected(jobs, returnedColumns={'maskCustomers': ['id', 'email'], 'maskOrders': ['id', 'owner']})

    assert any('orders.customer_id is masked with hash in domain customer' in message for _, message in _messages(report, 'warning'))


def test_a_job_whose_columns_do_not_line_up_is_not_guessed_at():
    report = _connected(_customersAndOrders('key', 'key'), targetColumns={'maskCustomers': ['id', 'email'], 'maskOrders': ['customer_id']})

    assert _messages(report, 'warning') == []


def _copies(customersQuery, ordersQuery='select * from orders', **customers: Any):
    return {
        'loadCustomers': _job(sourceQuery=customersQuery, unmasked=True, **customers),
        'loadOrders': _job(sourceQuery=ordersQuery, targetTableFinal='orders', unmasked=True),
        }


def _coverage(jobs):
    report = auditJobs(jobs, foreignKeys={'staging': [_foreignKey()]})
    return [(job, message) for job, message in _messages(report, 'warning') if 'copies only in part' in message]


def test_a_child_copied_whole_under_an_incremental_parent_is_flagged():
    jobs = _copies('select * from customers where updated_at > {{ watermark }}', watermarkColumn='updated_at', watermarkInitial='2026-01-01')

    assert _coverage(jobs) == [(
        'loadOrders',
        'in staging, orders.customer_id references customers, which loadCustomers copies only in part (incremental on updated_at), '
        'but loadOrders is not limited to the rows it copies, so the copy can reference rows it lacks. Limit loadOrders to rows whose '
        'customers loadCustomers copies, have loadCustomers also select what loadOrders references, or copy customers whole')]


def test_a_filtered_parent_is_flagged_too():
    (message,) = [message for _, message in _coverage(_copies("select * from customers WHERE region = 'eu'"))]

    assert '(its sourceQuery has a WHERE)' in message


def test_a_child_limited_by_its_parent_is_not_flagged():
    jobs = _copies("select * from customers where region = 'eu'",
                   ordersQuery="select * from orders o where exists (select 1 from app.\"CUSTOMERS\" c where c.id = o.customer_id "
                               "and c.region = 'eu')")

    assert _coverage(jobs) == []


def test_a_parent_that_also_selects_what_its_children_reference_is_not_flagged():
    jobs = _copies('select * from customers c where c.updated_at > {{ watermark }} or exists '
                   '(select 1 from orders o where o.customer_id = c.id and o.updated_at > {{ watermark }})',
                   watermarkColumn='updated_at', watermarkInitial='2026-01-01')

    assert _coverage(jobs) == []


def test_a_parent_copied_whole_is_not_flagged():
    assert _coverage(_copies('select * from customers', ordersQuery="select * from orders where status = 'open'")) == []


def test_a_similar_table_name_does_not_count_as_a_mention():
    jobs = _copies("select * from customers where region = 'eu'", ordersQuery='select * from orders join customers_archive a on 1 = 1')

    assert len(_coverage(jobs)) == 1


def test_a_table_referencing_itself_is_left_to_subset():
    from bauta.database.dialects import ForeignKey

    jobs = {'loadEmployees': _job(sourceQuery="select * from employees where site = 'x'", targetTableFinal='employees', unmasked=True)}
    report = auditJobs(jobs, foreignKeys={'staging': [ForeignKey('employees', ('manager_id',), 'employees', ('id',), 'fk_manager')]})

    assert report['findings'] == []


def test_coverage_needs_foreign_keys():
    jobs = _copies('select * from customers where updated_at > {{ watermark }}', watermarkColumn='updated_at', watermarkInitial='2026-01-01')

    assert auditJobs(jobs)['findings'] == []


def _ordered(orders: Any = None, **customers: Any):
    return {
        'loadCustomers': _job(**customers),
        'loadOrders': _job(sourceQuery='select * from orders', targetTableFinal='orders', **(orders or {})),
        }


def _ordering(jobs):
    report = auditJobs(jobs, foreignKeys={'staging': [_foreignKey()]})
    return [(job, message) for job, message in _messages(report, 'warning') if 'loaded yet' in message or 'inactive' in message]


def test_a_child_that_does_not_wait_for_its_parent_is_flagged():
    assert _ordering(_ordered()) == [(
        'loadOrders',
        'in staging, orders.customer_id references customers, but loadOrders does not wait for loadCustomers, directly or through its '
        'other predecessors, so it can load rows whose customers are not loaded yet. Add loadCustomers to its predecessors')]


def test_a_child_that_waits_for_its_parent_is_not_flagged():
    assert _ordering(_ordered({'predecessors': ['loadCustomers']})) == []


def test_waiting_through_another_job_counts():
    jobs = _ordered({'predecessors': ['loadRegions']})
    jobs['loadRegions'] = _job(sourceQuery='select * from regions', targetTableFinal='regions', predecessors=['loadCustomers'])

    assert _ordering(jobs) == []


def test_waiting_through_an_inactive_job_does_not_count():
    jobs = _ordered({'predecessors': ['loadRegions']})
    jobs['loadRegions'] = _job(sourceQuery='select * from regions', targetTableFinal='regions', predecessors=['loadCustomers'], active=False)

    (message,) = [message for _, message in _ordering(jobs)]
    assert 'loadOrders does not wait for loadCustomers' in message


def test_a_parent_with_a_longer_refresh_is_flagged():
    (message,) = [message for _, message in _ordering(_ordered({'predecessors': ['loadCustomers'], 'refresh': 5}, refresh=60))]

    assert message == ("in staging, orders.customer_id references customers, and loadOrders waits for loadCustomers only in cycles that "
                       "loadCustomers run in, since their refresh is longer than loadOrders's, so in the others it can load rows whose "
                       "customers are not loaded yet. Give them the same refresh")


def test_a_longer_refresh_between_them_is_named():
    jobs = _ordered({'predecessors': ['loadRegions']})
    jobs['loadRegions'] = _job(sourceQuery='select * from regions', targetTableFinal='regions', predecessors=['loadCustomers'], refresh=60)

    (message,) = [message for _, message in _ordering(jobs)]
    assert 'only in cycles that loadRegions run in' in message


def test_a_parent_with_the_same_or_a_shorter_refresh_is_not_flagged():
    assert _ordering(_ordered({'predecessors': ['loadCustomers'], 'refresh': 60}, refresh=60)) == []
    assert _ordering(_ordered({'predecessors': ['loadCustomers'], 'refresh': 60}, refresh=5)) == []


def test_an_inactive_parent_is_flagged():
    (message,) = [message for _, message in _ordering(_ordered({'predecessors': ['loadCustomers']}, active=False))]

    assert message == ('in staging, orders.customer_id references customers, but loadCustomers, which loads it, is inactive, '
                       'so loadOrders loads rows whose customers are not loaded')


def test_an_inactive_child_is_not_flagged():
    assert _ordering(_ordered({'active': False})) == []


def test_a_predecessor_outside_the_audited_jobs_is_not_guessed_at():
    assert _ordering(_ordered({'predecessors': ['loadRegions']})) == []


def _swapped(**customers: Any):
    fields = dict(insertStrategy='swap', targetTableStage='customers_stage')
    fields.update(customers)
    return {
        'loadCustomers': _job(**fields),
        'loadOrders': _job(sourceQuery='select * from orders', targetTableFinal='orders', predecessors=['loadCustomers']),
        }


def _swaps(jobs, foreignKey=None):
    report = auditJobs(jobs, declaredForeignKeys={'staging': [foreignKey or _foreignKey()]})
    return _messages(report, 'error')


def test_a_referenced_table_loaded_by_swap_is_an_error():
    assert _swaps(_swapped()) == [(
        'loadCustomers',
        'in staging, orders.customer_id references customers, which loadCustomers replaces by swap. The key stays on the table it was '
        'declared on, which the swap renames to customers_stage, so it stops checking customers, and the next run cannot empty '
        'customers_stage. Recreate the key on customers in postTargetAdhocQueries, or load customers with upsert and a stage table')]


def test_a_key_an_earlier_swap_left_on_the_stage_is_an_error():
    from bauta.database.dialects import ForeignKey

    (message,) = [message for _, message in _swaps(_swapped(), ForeignKey('orders', ('customer_id',), 'customers_stage', ('id',), 'fk'))]

    assert message.startswith('in staging, orders.customer_id references customers_stage, the stage table of loadCustomers, as an '
                              'earlier swap leaves it, so it does not check customers, and the next run cannot empty customers_stage')


def test_keys_recreated_after_the_swap_are_not_flagged():
    jobs = _swapped(postTargetAdhocQueries=['alter table orders drop constraint fk_orders_customers',
                                            'alter table orders add constraint fk_orders_customers foreign key (customer_id) '
                                            'references customers (id)'])

    assert _swaps(jobs) == []


def test_a_referenced_table_loaded_by_upsert_is_not_flagged():
    assert _swaps(_swapped(insertStrategy='upsert')) == []
    assert _swaps(_swapped(insertStrategy='upsert', targetTableStage=None)) == []


def test_a_swapped_table_referencing_itself_is_not_flagged():
    from bauta.database.dialects import ForeignKey

    assert _swaps(_swapped(), ForeignKey('customers', ('referrer_id',), 'customers', ('id',), 'fk')) == []


def test_keys_only_the_source_declares_do_not_count_for_a_swap():
    report = auditJobs(_swapped(), foreignKeys={'staging': [_foreignKey()]})

    assert _messages(report, 'error') == []


def test_a_swapped_table_that_declares_keys_is_warned_about():

    jobs = {'loadOrders': _job(sourceQuery='select * from orders', targetTableFinal='orders', insertStrategy='swap',
                               targetTableStage='orders_stage', unmasked=True)}
    report = auditJobs(jobs, declaredForeignKeys={'staging': [_foreignKey()]})

    assert _messages(report, 'warning') == [(
        'loadOrders',
        'in staging, orders declares foreign key(s) customer_id -> customers, but loadOrders replaces it by swap with orders_stage, '
        'which declares none, so after a run the copy stops enforcing them. Recreate them on orders in postTargetAdhocQueries, or load '
        'it with upsert and a stage table')]


def test_keys_recreated_after_a_swap_of_the_child_are_not_flagged():

    jobs = {'loadOrders': _job(sourceQuery='select * from orders', targetTableFinal='orders', insertStrategy='swap',
                               targetTableStage='orders_stage', unmasked=True,
                               postTargetAdhocQueries=['alter table orders add constraint fk foreign key (customer_id) references customers (id)'])}

    assert auditJobs(jobs, declaredForeignKeys={'staging': [_foreignKey()]})['findings'] == []


def test_a_key_and_its_reference_both_shuffled_are_flagged():
    """Matching policies aren't enough: shuffle moves values between rows, so
    every reference ends up pointing at another row.
    """
    shuffled = {'strategy': 'shuffle', 'domain': 'customer'}
    report = _connected(_customersAndOrders(shuffled, shuffled))

    (message,) = [message for _, message in _messages(report, 'warning') if 'shuffle' in message]
    assert message == ('in staging, orders.customer_id and customers.id, which it references, are both masked with shuffle, which moves '
                       'values between rows rather than mapping them, so the copied references will point at other rows. Mask a key and '
                       'its references with key or fpe')


def test_a_parent_limited_without_a_where_is_still_partial():
    for query, reason in (('select * from customers order by id limit 50', 'its sourceQuery has a LIMIT'),
                          ('select top 50 * from customers', 'its sourceQuery has a TOP'),
                          ('select * from customers order by id fetch first 50 rows only', 'its sourceQuery has a FETCH FIRST'),
                          ('select c.* from customers c join regions r on r.code = c.region_code', 'its sourceQuery joins another table')):
        (message,) = [message for _, message in _coverage(_copies(query))]

        assert '({})'.format(reason) in message
