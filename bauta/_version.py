"""This package's version, here alone: pyproject.toml reads it to build, and
bauta reads it -- for a manifest, `bauta --version`, and the native masker,
which must be the same version -- rather than what pip recorded, which an
editable install keeps from whenever it was installed.
"""

__version__ = '0.2.10'
