"""What a configuration reads from outside itself: ${NAME} and ${file:...}
references, expanded before validation, and the passwordCommand run when a
connection opens. Secrets pass through here and never appear in an error.
"""
from __future__ import annotations

import os
import re
import shlex
import subprocess
from typing import Any, List, Union


class ConfigurationError(Exception):
    """Raised when user-supplied YAML configuration fails validation."""


# ${NAME}, ${NAME:-default} or ${file:/path}; $${...} escapes one.
ENVIRONMENT_VARIABLE = re.compile(r'(\$?)\$\{(?:file:([^}]+)|([A-Za-z_][A-Za-z0-9_]*)(?::-([^}]*))?)\}')


def _expand(value: Any, missing: List[str]) -> Any:
    """Recursive worker: collects what couldn't be read rather than raising on the first."""

    if isinstance(value, dict):
        return {key: _expand(item, missing) for key, item in value.items()}
    if isinstance(value, list):
        return [_expand(item, missing) for item in value]
    if not isinstance(value, str):
        return value

    def replace(match: Any) -> str:
        escape, path, name, default = match.groups()

        if escape:
            return match.group(0)[1:]

        if path is not None:
            try:
                with open(path) as file:
                    # Secret files usually end with a newline nobody meant as
                    # part of the secret.
                    return file.read().rstrip('\r\n')
            except OSError as error:
                missing.append('file {} ({})'.format(path, error.strerror or error))
                return ''

        if name in os.environ:
            return os.environ[name]
        if default is not None:
            return default

        missing.append('${}'.format(name))

        return ''

    return ENVIRONMENT_VARIABLE.sub(replace, value)


def expandEnvironmentVariables(value: Any) -> Any:
    """Recursively replace ${NAME} and ${file:/path} in a loaded configuration:

        password: ${PROD_DB_PASSWORD}
        port: ${PROD_DB_PORT:-5432}
        key: ${file:/run/secrets/masking-key}

    A file reference reads the file less a trailing newline, as mounted
    secrets are written. An unset variable with no default, or an unreadable
    file, raises ConfigurationError rather than expanding to an empty string,
    naming every one at once. Escape a literal ${...} as $${...}.
    """

    missing: List[str] = []
    expanded = _expand(value, missing)

    if missing:
        raise ConfigurationError(
            'configuration references value(s) that could not be read: {}. '
            'Set each variable, or give it a default with ${{NAME:-value}} (never for a secret).'.format(', '.join(sorted(set(missing)))))

    return expanded


PASSWORD_COMMAND_TIMEOUT_SECONDS = 60


class PasswordCommandError(RuntimeError):
    """passwordCommand failed. Not a ConfigurationError: the usual cause -- a
    token service that's briefly unreachable -- is worth a retry.
    """


def splitPasswordCommand(command: Union[str, List[str]]) -> List[str]:
    """The program and arguments a passwordCommand runs. A list is taken as it
    is; a string is split like a shell would split it, but no shell is involved.

    Raises ValueError for a command with no program, or a string a shell
    couldn't split, such as one with an unterminated quote.
    """

    if isinstance(command, str):
        try:
            arguments = shlex.split(command)
        except ValueError as error:
            raise ValueError('passwordCommand cannot be split into a program and arguments: {}'.format(error)) from None
    else:
        arguments = list(command)

    if not arguments or not arguments[0].strip():
        raise ValueError('passwordCommand names no program to run')

    return arguments


def runPasswordCommand(command: Union[str, List[str]]) -> str:
    """Runs a passwordCommand and returns what it printed, stripped. The
    output, being the secret, never appears in an error.
    """

    try:
        arguments = splitPasswordCommand(command)
    except ValueError as error:
        raise ConfigurationError(str(error)) from None

    try:
        completed = subprocess.run(arguments, capture_output=True, text=True, timeout=PASSWORD_COMMAND_TIMEOUT_SECONDS)
    except (OSError, subprocess.TimeoutExpired) as error:
        raise PasswordCommandError('passwordCommand {} could not run: {}'.format(arguments[0], error)) from None

    if completed.returncode != 0:
        raise PasswordCommandError('passwordCommand {} exited with status {}: {}'.format(
            arguments[0], completed.returncode, completed.stderr.strip()[-500:]))

    password = completed.stdout.strip()
    if not password:
        raise PasswordCommandError('passwordCommand {} printed nothing'.format(arguments[0]))

    return password
