"""Keeps the documentation honest.

Prose is the one artifact nothing else checks, and the split into docs/ is
exactly where it rots: a heading renamed without its links, or a configuration
field added to a model without being written up. These are cheap to check, so
they're checked.
"""
import importlib.util
import re
from pathlib import Path

import typing

import pytest

from bauta.cli import _buildParser
from bauta.configuration import (Configuration, ConnectionConfig, DataJobConfig, DataJobsFile, DiscoveryRulesFile, MaskingConfig, NameRuleConfig,
                                 ValueRuleConfig, expandEnvironmentVariables)
from bauta.masking import STRATEGIES

ROOT = Path(__file__).resolve().parents[2]
DOCUMENTS = [ROOT / 'README.md', ROOT / 'CHANGELOG.md', ROOT / 'CONTRIBUTING.md', ROOT / 'example' / 'README.md'] + sorted((ROOT / 'docs').rglob('*.md'))
CONNECTIONS_DOC = ROOT / 'docs' / 'reference' / 'connections.md'
JOBS_DOC = ROOT / 'docs' / 'reference' / 'jobs.md'
MASKED_JOB_DOC = ROOT / 'docs' / 'guides' / 'mask-a-table.md'
DISCOVERY_DOC = ROOT / 'docs' / 'guides' / 'propose-a-policy.md'
STRATEGIES_DOC = ROOT / 'docs' / 'reference' / 'strategies.md'


def _anchors(path: Path) -> set:
    """GitHub's heading slugs: lowercase, punctuation dropped, spaces to hyphens."""
    slugs = set()
    for line in path.read_text().splitlines():
        match = re.match(r'^#{1,6}\s+(.*)$', line)
        if match:
            slugs.add(re.sub(r'[^a-z0-9 _-]', '', match.group(1).strip().lower()).replace(' ', '-'))
    return slugs


def _internalLinks():
    for document in DOCUMENTS:
        for target, anchor in re.findall(r'\]\(([^)#\s]*)(?:#([^)\s]+))?\)', document.read_text()):
            if not target.startswith('http'):
                yield document, target, anchor


@pytest.mark.parametrize('document,target,anchor', list(_internalLinks()),
                         ids=lambda value: str(value.relative_to(ROOT)) if isinstance(value, Path) else value)
def test_every_internal_link_resolves(document, target, anchor):
    resolved = (document.parent / target).resolve() if target else document.resolve()

    assert resolved.exists(), '{} links to missing {}'.format(document.relative_to(ROOT), target)

    if anchor and resolved.suffix == '.md':
        assert anchor in _anchors(resolved), '{} links to missing heading #{} in {}'.format(
            document.relative_to(ROOT), anchor, resolved.relative_to(ROOT))


@pytest.mark.parametrize('model,document', [(connection, CONNECTIONS_DOC) for connection in typing.get_args(typing.get_args(ConnectionConfig)[0])] + [
                                            (DataJobConfig, JOBS_DOC),
                                            (DataJobsFile, JOBS_DOC), (MaskingConfig, MASKED_JOB_DOC),
                                            (DiscoveryRulesFile, DISCOVERY_DOC), (NameRuleConfig, DISCOVERY_DOC), (ValueRuleConfig, DISCOVERY_DOC)],
                         ids=lambda value: getattr(value, '__name__', None) or value.name)
def test_every_configuration_field_is_documented(model, document):
    """A field added to a model without a line in the reference is the most
    common way a field reference goes stale, and the hardest to notice.
    """
    reference = document.read_text()

    undocumented = [name for name in model.model_fields if '`{}`'.format(name) not in reference]

    assert not undocumented, '{} field(s) missing from {}: {}'.format(model.__name__, document.name, ', '.join(undocumented))


def test_every_built_in_discovery_rule_is_documented():
    """`exclude` takes these names, so each must be findable."""
    from bauta.generate.builtinDiscovery import RULE_NAMES

    reference = DISCOVERY_DOC.read_text()

    assert not [name for name in sorted(RULE_NAMES) if '`{}`'.format(name) not in reference]


def test_every_masking_strategy_and_option_is_documented():
    reference = STRATEGIES_DOC.read_text()

    for name, strategy in STRATEGIES.items():
        assert '`{}`'.format(name) in reference, 'strategy {} is missing from {}'.format(name, STRATEGIES_DOC.name)
        for option in strategy.OPTIONS:
            assert '`{}`'.format(option) in reference, 'option {} of {} is missing from {}'.format(option, name, STRATEGIES_DOC.name)


def test_the_masked_job_in_the_masking_guide_is_valid_configuration(monkeypatch):
    import yaml

    monkeypatch.setenv('MASKING_KEY', 'a-documentation-masking-key')
    firstBlock = MASKED_JOB_DOC.read_text().split('```yaml\n', 1)[1].split('```', 1)[0]

    jobsFile = Configuration.validateJobConfiguration({'workers': 1, 'jobs': expandEnvironmentVariables(yaml.safe_load(firstBlock))}, DataJobsFile)

    assert jobsFile.jobs['maskCustomers'].masking.columns['notes'] == {'strategy': 'null'}


def test_every_documented_subcommand_exists():
    """The README lists the commands; the parser is what actually runs."""
    readme = (ROOT / 'README.md').read_text()
    documented = set(re.findall(r'^bauta ([a-z][a-z-]*)', readme, flags=re.MULTILINE))

    subparsers = next(action for action in _buildParser()._actions if action.dest == 'command')
    real = set(subparsers.choices)

    assert documented, 'the README documents no subcommands'
    assert documented == real, 'README documents {}, the CLI has {}'.format(sorted(documented), sorted(real))


def test_every_documented_flag_exists():
    documented = set()
    for document in DOCUMENTS:
        documented.update(re.findall(r'\| `(--[a-z-]+)', document.read_text()))

    real = set()
    for action in _buildParser()._actions:
        real.update(action.option_strings)
        if action.dest == 'command':
            for subparser in action.choices.values():
                for option in subparser._actions:
                    real.update(option.option_strings)

    missing = documented - real

    assert documented
    assert not missing, 'the docs document flags the CLI does not have: {}'.format(sorted(missing))


def _commandPages():
    specification = importlib.util.spec_from_file_location('commands', ROOT / 'website' / 'commands.py')
    module = importlib.util.module_from_spec(specification)
    specification.loader.exec_module(module)

    return module.pages()


def test_every_command_page_matches_the_parser():
    """The pages under docs/reference/commands/ are generated from the parser,
    so a flag added, removed or reworded without regenerating them fails here.
    """
    expected = _commandPages()
    directory = ROOT / 'docs' / 'reference' / 'commands'

    stale = sorted(path.name for path, text in expected.items() if not path.exists() or path.read_text() != text)
    extra = sorted(path.name for path in set(directory.glob('*.md')) - set(expected))

    assert not stale and not extra, 'run `python website/commands.py`: {}'.format(', '.join(stale + extra))


def test_every_page_is_in_the_site_navigation():
    """A page left out of mkdocs.yml's nav is built but linked from nowhere."""
    import yaml

    class _Loader(yaml.SafeLoader):
        pass

    # mkdocs.yml names Python objects for the Markdown extensions
    _Loader.add_multi_constructor('tag:yaml.org,2002:python/', lambda loader, suffix, node: None)
    navigation = yaml.load((ROOT / 'mkdocs.yml').read_text(), Loader=_Loader)['nav']

    def walk(entries):
        for entry in entries:
            value = next(iter(entry.values())) if isinstance(entry, dict) else entry
            if isinstance(value, list):
                yield from walk(value)
            else:
                yield value

    listed = set(walk(navigation))
    pages = {str(path.relative_to(ROOT / 'docs')) for path in (ROOT / 'docs').rglob('*.md')}

    assert not pages - listed, 'not in mkdocs.yml nav: {}'.format(sorted(pages - listed))
    assert not {page for page in listed if page.endswith('.md')} - pages - GENERATED_PAGES, 'mkdocs.yml nav names missing pages'


# Written into the site at build time by website/hooks.py, from files outside docs/
GENERATED_PAGES = {'get-started/examples.md', 'project/changelog.md', 'project/contributing.md'}
