"""What `audit --connect` makes of the rows it samples: kept columns whose
values look like personal data, and columns sharing a domain that hold its
values differently. No sampled value may appear in the report.
"""
import functools
import json
from typing import Any

from bauta.configuration import DataJobConfig
from bauta.review.audit import auditJobs, renderAudit
from tests.jobConfigs import dataJob

KEY = 'an-audit-sample-test-masking-key'

_job = functools.partial(dataJob, sourceConnection='prod', targetConnection='staging')


def _masked(columns, **overrides: Any) -> DataJobConfig:
    return _job(masking={'key': KEY, 'columns': columns}, **overrides)


def _messages(report, severity=None):
    return [(finding['job'], finding['message']) for finding in report['findings'] if severity in (None, finding['severity'])]


def _assertNoValueIn(report, *values):
    text = json.dumps(report, default=str) + renderAudit(report)
    for value in values:
        assert str(value) not in text


SSNS = [('C{}'.format(index), '123-45-{:04d}'.format(6789 + index)) for index in range(20)]


def test_a_kept_column_whose_values_look_like_personal_data_is_flagged_whatever_its_name():
    """`SELECT ssn AS ref` with `ref: keep` names nothing personal."""
    report = auditJobs({'copyAccounts': _masked({'id': 'keep', 'ref': 'keep'})},
                       returnedColumns={'copyAccounts': ['id', 'ref']}, samples={'copyAccounts': SSNS})

    assert _messages(report, 'warning') == [('copyAccounts', 'column ref is kept unmasked, but sampled values look like national identifiers')]
    assert report['jobs'][0]['sampledRows'] == 20
    _assertNoValueIn(report, '123-45-6789')


def test_a_kept_column_named_for_what_it_holds_is_flagged_once():
    report = auditJobs({'copyAccounts': _masked({'id': 'keep', 'ssn': 'keep'})},
                       returnedColumns={'copyAccounts': ['id', 'ssn']}, samples={'copyAccounts': SSNS})

    assert [message for _, message in _messages(report)] == ['column ssn is kept unmasked, but its name suggests a government identifier; '
                                                             'key keeps it unique and shaped']


def test_kept_uuids_are_noted_rather_than_warned_about():
    rows = [('9b2f0c1e-4d5a-4b6c-8d7e-0f1a2b3c4d{:02d}'.format(index),) for index in range(10)]

    report = auditJobs({'copyOrders': _masked({'id': 'keep'})}, returnedColumns={'copyOrders': ['id']}, samples={'copyOrders': rows})

    assert [severity for severity in report['summary'] if report['summary'][severity]] == ['info']
    _assertNoValueIn(report, '9b2f0c1e')


def test_kept_json_holding_personal_data_is_flagged():
    rows = [({'contact': {'email': 'ann{}@corp.example'.format(index)}},) for index in range(10)]

    report = auditJobs({'copyProfiles': _masked({'profile': 'keep'})}, returnedColumns={'copyProfiles': ['profile']},
                       samples={'copyProfiles': rows})

    ((_, message),) = _messages(report, 'warning')
    assert message.startswith('column profile is kept unmasked, but sampled JSON documents hold personal data at contact.email')
    _assertNoValueIn(report, 'ann0@corp.example')


def test_long_kept_text_is_not_flagged_by_its_length_alone():
    rows = [('A sturdy oak table, hand finished, seats six, ships flat in two boxes with every tool needed. ' * 2,)] * 10

    report = auditJobs({'copyProducts': _masked({'blurb': 'keep'})}, returnedColumns={'copyProducts': ['blurb']}, samples={'copyProducts': rows})

    assert report['findings'] == []


def test_an_unmasked_job_whose_values_look_like_personal_data_is_an_error():
    report = auditJobs({'copyAccounts': _job()}, returnedColumns={'copyAccounts': ['id', 'ref']}, samples={'copyAccounts': SSNS})

    ((_, message),) = _messages(report, 'error')
    assert 'ref (sampled values look like national identifiers)' in message
    _assertNoValueIn(report, '123-45-6790')


def test_without_samples_nothing_changes():
    report = auditJobs({'copyAccounts': _masked({'id': 'keep', 'ref': 'keep'})}, returnedColumns={'copyAccounts': ['id', 'ref']})

    assert report['findings'] == [] and report['jobs'][0]['sampledRows'] is None


# --- columns sharing a domain ---------------------------------------------------

def _shared(orders, tickets, ticketPolicy=None, orderPolicy=None):
    jobs = {'maskOrders': _masked({'customer_id': orderPolicy or {'strategy': 'key', 'domain': 'customers'}}),
            'maskTickets': _masked({'customer_ref': ticketPolicy or {'strategy': 'key', 'domain': 'customers'}})}
    return auditJobs(jobs, returnedColumns={'maskOrders': ['customer_id'], 'maskTickets': ['customer_ref']},
                     samples={'maskOrders': [(value,) for value in orders], 'maskTickets': [(value,) for value in tickets]})


def test_a_domain_holding_ids_as_numbers_and_as_text_is_flagged():
    report = _shared([1234567, 2345678], ['1234567', '2345678'])

    ((_, message),) = _messages(report, 'warning')
    assert 'holds ids as numbers in maskOrders.customer_id and as text in maskTickets.customer_ref' in message
    assert 'normalize: [integer]' in message
    _assertNoValueIn(report, 1234567)


def test_a_domain_whose_text_is_normalized_to_integers_is_not_flagged():
    report = _shared([1234567], ['1234567'], ticketPolicy={'strategy': 'key', 'domain': 'customers', 'normalize': ['integer']})

    assert report['findings'] == []


def test_text_that_spells_no_integer_is_not_compared_with_numbers():
    report = _shared([1234567], ['C-1234567'])

    assert report['findings'] == []


def test_a_domain_padded_in_one_column_and_not_another_is_flagged():
    report = _shared(['AB12    ', 'CD34    '], ['AB12', 'CD34'], orderPolicy={'strategy': 'hash', 'domain': 'customers'},
                     ticketPolicy={'strategy': 'hash', 'domain': 'customers'})

    ((_, message),) = _messages(report, 'warning')
    assert 'sampled values of maskOrders.customer_id have leading or trailing spaces' in message and 'normalize: [strip]' in message


def test_a_padded_domain_that_strips_is_not_flagged():
    stripped = {'strategy': 'key', 'domain': 'customers', 'normalize': ['strip']}

    assert _shared(['AB12    '], ['AB12'], orderPolicy=stripped, ticketPolicy=stripped)['findings'] == []
    # One column alone can't disagree with itself.
    report = auditJobs({'maskOrders': _masked({'customer_id': {'strategy': 'key', 'domain': 'customers'}})},
                       returnedColumns={'maskOrders': ['customer_id']}, samples={'maskOrders': [('AB12    ',)]})
    assert report['findings'] == []
