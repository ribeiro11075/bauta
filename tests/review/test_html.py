"""`audit` and `coverage` as one HTML page: self-contained, escaped, and
holding nothing the text report doesn't.
"""
import re

import pytest

from bauta.review.audit import ConnectedFacts, auditJobs
from bauta.review.coverage import coverageReport
from bauta.review.html import renderAuditHtml, renderCoverageHtml
from tests.jobConfigs import dataJob

KEY = 'an-html-report-masking-key-never-shown'


def _audit():
    jobs = {
        'maskCustomers': dataJob(sourceQuery='select * from customers', masking={'key': KEY, 'columns': {'id': 'keep', 'email': 'keep'},
                                                                                  'defaultStrategy': 'hash'}),
        'copy<script>alert(1)</script>': dataJob(sourceQuery='select * from orders', targetTableFinal='orders'),
        }

    return auditJobs(jobs, ConnectedFacts(returnedColumns={'maskCustomers': ['id', 'email', 'phone'], 'copy<script>alert(1)</script>': ['id', 'email']}, encryption={'source': False, 'target': True}))


def _coverage():
    jobs = {'maskCustomers': dataJob(sourceQuery='select * from customers', masking={'key': KEY, 'columns': {'id': 'keep'}}),
            'copyOrders': dataJob(sourceQuery='select * from orders', unmasked=True)}

    return coverageReport('source', ['customers', 'orders', 'people', 'audit_log', 'a&b'], jobs,
                          acknowledged={'audit_log': 'internal <only>'}, columns={'people': ['id', 'email']})


@pytest.mark.parametrize('page', [lambda: renderAuditHtml(_audit(), version='9.9'), lambda: renderCoverageHtml(_coverage(), '2026-10-05T00:00:00+00:00')])
def test_a_page_is_self_contained_and_follows_the_readers_theme(page):
    rendered = page()

    assert rendered.startswith('<!doctype html>')
    assert '@media (prefers-color-scheme: dark)' in rendered and 'color-scheme: light dark' in rendered
    assert not re.search(r'<script|<link|<img|src=|href=|@import|url\(', rendered, re.IGNORECASE)
    assert 'http' not in rendered


def test_the_audit_page_summarizes_then_shows_each_job_and_its_findings():
    rendered = renderAuditHtml(_audit(), version='9.9')

    assert 'by bauta 9.9' in rendered
    assert re.search(r'<strong>1</strong><span>errors</span>', rendered)
    assert 'Not ready: 1 error must be resolved' in rendered
    assert '<code>maskCustomers</code>' in rendered and 'column email is kept unmasked, but its' in rendered
    assert '<td class="name">phone</td><td><code>hash</code></td>' in rendered
    assert 'NOT encrypted' in rendered


def test_the_audit_page_escapes_names_and_never_holds_the_key():
    rendered = renderAuditHtml(_audit())

    assert '<script>' not in rendered
    assert 'copy&lt;script&gt;alert(1)&lt;/script&gt;' in rendered
    assert KEY not in rendered


def test_the_coverage_page_puts_what_is_not_covered_first_with_its_personal_columns():
    rendered = renderCoverageHtml(_coverage())

    sections = re.findall(r'<h2>([^<]+) <span', rendered)
    assert sections == ['Not covered', 'Copied as it stands', 'Copied and masked', 'Not copied, declared']
    assert 'Columns that look like personal data: <code>email</code>' in rendered
    assert 'internal &lt;only&gt;' in rendered and 'a&amp;b' in rendered
    assert KEY not in rendered


def test_a_coverage_page_with_everything_covered_says_so():
    jobs = {'copyAll': dataJob(sourceQuery='select * from customers', unmasked=True)}

    rendered = renderCoverageHtml(coverageReport('source', ['customers'], jobs))

    assert 'Every table is either copied by a job or declared as not copied.' in rendered
