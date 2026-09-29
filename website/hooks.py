"""MkDocs hooks: what the site needs that plain docs/ can't say by itself.

The Markdown in docs/ is written to read on GitHub too, so its links are
relative paths in the repository: to CHANGELOG.md, to example/, to a module in
bauta/. A site has none of those files. So:

- CHANGELOG.md, CONTRIBUTING.md and example/README.md become pages of the
  site, generated at build time, so they have one copy each.
- Any other link out of docs/ goes to that file on GitHub, at the ref the site
  is built from (BAUTA_DOCS_REF: a release tag, or master).
"""
import os
import posixpath
import re
from pathlib import Path

from mkdocs.structure.files import File

REPOSITORY = 'https://github.com/ribeiro11075/bauta'
ROOT = Path(__file__).resolve().parents[1]

# Repository file -> (the page it becomes, its title there, or None to keep its own)
OUTSIDE = {
    'example/README.md': ('get-started/examples.md', 'Examples'),
    'CHANGELOG.md': ('project/changelog.md', None),
    'CONTRIBUTING.md': ('project/contributing.md', None),
    }

FENCE = re.compile(r'^\s*(```|~~~)')
LINK = re.compile(r'\]\(([^)#\s]*)(#[^)\s]+)?\)')


def _ref() -> str:

    return os.environ.get('BAUTA_DOCS_REF', 'master')


def rewrite(markdown: str, source: str, page: str) -> str:
    """Rewrites markdown's links, written relative to the repository file
    source, for the site page page (a path under docs/).
    """
    lines, inFence = [], False
    for line in markdown.split('\n'):
        if FENCE.match(line):
            inFence = not inFence
        if not inFence:
            line = LINK.sub(lambda match: _link(match, source, page), line)
        lines.append(line)

    return '\n'.join(lines)


def _link(match: 're.Match[str]', source: str, page: str) -> str:

    target, anchor = match.group(1), match.group(2) or ''
    if not target or re.match(r'^[a-z]+:', target):
        return match.group(0)

    path = posixpath.normpath(posixpath.join(posixpath.dirname(source), target))
    if path.startswith('docs/'):
        destination = path[len('docs/'):]
    elif path in OUTSIDE:
        destination = OUTSIDE[path][0]
    else:
        kind = 'tree' if (ROOT / path).is_dir() else 'blob'
        return ']({}/{}/{}/{}{})'.format(REPOSITORY, kind, _ref(), path, anchor)

    return ']({}{})'.format(posixpath.relpath(destination, posixpath.dirname(page) or '.'), anchor)


def on_files(files, config):

    for source, (page, title) in OUTSIDE.items():
        text = (ROOT / source).read_text()
        if title:
            text = re.sub(r'^# .*$', '# ' + title, text, count=1, flags=re.MULTILINE)
        files.append(File.generated(config, page, content=rewrite(text, source, page)))

    return files


def on_page_markdown(markdown, page, config, files):

    if page.file.generated_by:
        return markdown

    return rewrite(markdown, 'docs/' + page.file.src_uri, page.file.src_uri)


def on_page_context(context, page, config, nav):

    # A generated page's source is the file outside docs/ it came from
    for source, (generated, _) in OUTSIDE.items():
        if page.file.src_uri == generated:
            page.edit_url = '{}/edit/{}/{}'.format(REPOSITORY, _ref(), source)

    return context
