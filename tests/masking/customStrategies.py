"""A masking strategy defined outside the package, as a policy can name one."""
from bauta.masking import Strategy


class Initials(Strategy):
    """Keeps a name's initials, keyed so the rest is unrecoverable."""

    OPTIONS = {'separator': str}

    def mask(self, value):
        separator = self.options.get('separator', '.')
        return separator.join(word[0] for word in str(value).split()) + separator + self.keyedHash.digest(str(value).encode()).hex()[:4]


class Verbatim(Strategy):
    """Returns every value as it was given -- a custom `keep`."""

    KEYED = False
    PASSTHROUGH = True

    def maskColumn(self, values, chunkIndex):
        return list(values)


class NotAStrategy:
    pass
