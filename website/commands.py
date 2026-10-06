"""Writes docs/reference/commands/ from the command's own parser.

A flag documented by hand drifts from the flag that runs, so each command's
page is generated from `_buildParser()`, and a test fails when a page no longer
matches it. After changing a flag:

    python website/commands.py
"""
import argparse
import re
import sys
from pathlib import Path
from typing import Dict, List

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from bauta.cli import _buildParser  # noqa: E402

OUTPUT = ROOT / 'docs' / 'reference' / 'commands'

# Where each command is explained, relative to docs/reference/commands/
GUIDES = {
    'run': ('Run on a schedule', '../../guides/run-on-a-schedule.md'),
    'validate': ('Validation', '../configuration.md#validation'),
    'jobs': ('Run on a schedule', '../../guides/run-on-a-schedule.md'),
    'history': ('Watch what ran', '../../guides/watch-what-ran.md#run-history'),
    'audit': ('Prove the copy is safe', '../../guides/prove-the-copy-is-safe.md#reviewing-policies-audit'),
    'coverage': ('Prove the copy is safe', '../../guides/prove-the-copy-is-safe.md#coverage-what-the-jobs-do-not-cover'),
    'verify-manifest': ('Keep a masking manifest', '../../guides/keep-a-manifest.md#sealing-and-verifying'),
    'verify-references': ('Copy a subset', '../../guides/copy-a-subset.md#verify-references-checking-the-copys-references'),
    'discover': ('Propose a policy', '../../guides/propose-a-policy.md'),
    'subset': ('Copy a subset', '../../guides/copy-a-subset.md'),
    'schema': ('Copy a subset', '../../guides/copy-a-subset.md#schema-creating-the-targets-tables'),
    'synthesize': ('Generate data', '../../guides/generate-data.md'),
    'clear': ('Copy a subset', '../../guides/copy-a-subset.md#clear-emptying-the-copy-before-a-refresh'),
    'bench': ('Make it faster', '../../guides/make-it-faster.md#measure-a-job'),
    }

HEADER = '<!-- Generated from the parser by website/commands.py. Change the flag, then run `python website/commands.py`. -->\n'


def _subcommands() -> Dict[str, argparse.ArgumentParser]:

    (subparsers,) = [action for action in _buildParser()._actions if isinstance(action, argparse._SubParsersAction)]

    return dict(subparsers.choices)


def _summaries() -> Dict[str, str]:

    (subparsers,) = [action for action in _buildParser()._actions if isinstance(action, argparse._SubParsersAction)]

    return {choice.dest: choice.help or '' for choice in subparsers._choices_actions}


def _options(parser: argparse.ArgumentParser) -> List[argparse.Action]:

    return [action for action in parser._actions if not isinstance(action, argparse._HelpAction)]


def _flags(action: argparse.Action) -> List[str]:

    return list(action.option_strings) or [action.dest]


def commonFlags() -> List[str]:
    """The flags every subcommand takes, documented once on the index."""

    sets = [{flag for action in _options(parser) for flag in action.option_strings} for parser in _subcommands().values()]

    return sorted(set.intersection(*sets))


def _sentence(text: str) -> str:

    text = text.strip()
    if not text:
        return ''
    first = text.split(' ', 1)[0]
    if first.islower():
        # Not a name such as chunkSize or SQL, whose case matters
        text = text[0].upper() + text[1:]

    return text if text.endswith(('.', ')')) else text + '.'


def _cell(text: str) -> str:

    return text.replace('|', '\\|')


def _row(action: argparse.Action) -> str:

    if action.option_strings:
        if action.choices:
            value = ' {' + ','.join(str(choice) for choice in action.choices) + '}'
        elif action.nargs == 0:
            value = ''
        else:
            value = ' ' + (action.metavar or action.dest.upper())
        flag = '`{}{}`'.format(' '.join(action.option_strings), value)
    else:
        flag = '`{}`'.format(action.metavar or action.dest.upper())

    helpText = action.help or ''
    if action.required:
        default = 'required'
    elif action.nargs == 0 and action.default in (False, None):
        default = 'off'
    elif action.default not in (None, argparse.SUPPRESS) and not isinstance(action.default, bool):
        default = '`{}`'.format(action.default)
        # The default has its own column
        helpText = re.sub(r'\s*\(?default: [^)]*\)?$', '', helpText)
    else:
        # A default the parser leaves to the code, written in the help instead
        stated = re.search(r'\s*\(default: ([^()]*(?:\([^()]*\)[^()]*)*)\)$', helpText)
        default = stated.group(1) if stated else ''
        helpText = helpText[:stated.start()] if stated else helpText

    if not helpText and action.choices:
        helpText = 'one of {}'.format(', '.join('`{}`'.format(choice) for choice in action.choices))

    return '| {} | {} | {} |'.format(flag, default, _cell(_sentence(helpText)))


def _usage(name: str, parser: argparse.ArgumentParser, common: List[str]) -> str:

    parts = ['bauta', name]
    for action in _options(parser):
        if action.required:
            parts.append('{} {}'.format(action.option_strings[0], action.metavar or action.dest.upper()))
        elif not action.option_strings:
            parts.append('[{}]'.format(action.metavar or action.dest.upper()))
    parts.append('[options]')

    return ' '.join(parts)


def commandPage(name: str, parser: argparse.ArgumentParser, summary: str, common: List[str]) -> str:

    own = [action for action in _options(parser) if not set(action.option_strings) & set(common)]
    title, link = GUIDES[name]
    lines = [
        HEADER,
        '# `bauta {}`'.format(name),
        '',
        _sentence(summary),
        '',
        '```',
        _usage(name, parser, common),
        '```',
        '',
        'See [{}]({}) for how to use it.'.format(title, link),
        '',
        '## Options',
        '',
        '| Flag | Default | Effect |',
        '| --- | --- | --- |',
        ]
    lines += [_row(action) for action in own]
    lines += ['', 'It also takes the [common flags](index.md#common-flags): {}.'.format(', '.join('`{}`'.format(flag) for flag in common)), '']

    return '\n'.join(lines)


def indexPage(summaries: Dict[str, str], common: List[str]) -> str:

    parser = _buildParser()
    anyParser = next(iter(_subcommands().values()))
    commonActions = [action for action in _options(anyParser) if set(action.option_strings) & set(common)]
    version = next(action for action in parser._actions if '--version' in action.option_strings)

    lines = [
        HEADER,
        '# Commands',
        '',
        '| Command | What it does |',
        '| --- | --- |',
        ]
    lines += ['| [`bauta {0}`]({0}.md) | {1} |'.format(name, _cell(_sentence(summary))) for name, summary in summaries.items()]
    lines += [
        '| `bauta --version` | {} |'.format(_sentence(version.help or '')),
        '',
        '## Exit codes',
        '',
        'Every command exits with one of these.',
        '',
        '| Exit code | Meaning |',
        '| --- | --- |',
        '| `0` | Every job completed, or the command did what it was asked. |',
        '| `1` | A job failed, or was skipped because a predecessor failed; the command failed on a database error; or a check (`audit`, `coverage`, `verify-manifest`, `verify-references`) found a problem. |',
        '| `2` | Invalid configuration or usage. |',
        '| `130` | Interrupted by a signal: running jobs finished, the rest were skipped. |',
        '',
        '## Common flags',
        '',
        'Every command takes these.',
        '',
        '| Flag | Default | Effect |',
        '| --- | --- | --- |',
        ]
    lines += [_row(action) for action in commonActions]
    lines.append('')

    return '\n'.join(lines)


def pages() -> Dict[Path, str]:

    common = commonFlags()
    summaries = _summaries()
    result = {OUTPUT / 'index.md': indexPage(summaries, common)}
    for name, parser in _subcommands().items():
        result[OUTPUT / '{}.md'.format(name)] = commandPage(name, parser, summaries[name], common)

    return result


def main() -> None:

    OUTPUT.mkdir(parents=True, exist_ok=True)
    expected = pages()
    for stale in set(OUTPUT.glob('*.md')) - set(expected):
        stale.unlink()
    for path, text in expected.items():
        path.write_text(text)
    print('Wrote {} page(s) to {}'.format(len(expected), OUTPUT.relative_to(ROOT)))


if __name__ == '__main__':
    main()
