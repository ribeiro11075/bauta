"""Where something is: a coordinate, moved by a distance on the ground, and an IP address, as another of its family.

Part of the strategies package; see its __init__.
"""
from __future__ import annotations

import decimal
import ipaddress
import math
from typing import Any, Dict, List, Sequence

from ..core import MaskingError, Strategy, canonical
from .common import _choiceOption, _integerOption, _textOption, _typeName


# Metres in a degree of latitude, and of longitude at the equator; a degree of
# longitude spans this times the cosine of its latitude.
_METRES_PER_DEGREE = 111320.0

# How far from zero each axis reaches, in degrees.
_AXIS_LIMITS = {'latitude': 90, 'longitude': 180}


class CoordinateStrategy(Strategy):
    """A latitude or a longitude moved along its axis by a keyed distance
    between half of `meters` and all of it, in a keyed direction, keyed on the
    value: every point is moved by a distance on the ground, wherever it is,
    where `number`'s variance moves a point near the equator or the prime
    meridian by next to nothing.

    A degree of longitude spans fewer metres away from the equator, so a
    longitude is moved by `meters` there unless `latitudeColumn` names the
    row's latitude, which converts the distance at that latitude. A latitude
    that would pass a pole moves the other way; a longitude wraps at 180.
    Floats come back as floats, and a Decimal at its own scale, moved at least
    one step.

    A value outside a latitude's or longitude's range fails the job: it is
    not a position in degrees, so moving it as one would not move it at all.
    A column of microdegrees, 40712776 for 40.712776, moved by a kilometre's
    worth of degrees changed in its ninth decimal place, and an integer column
    rounded it straight back. `scale` reads such a column: the value is
    degrees times `scale` (1000000 for microdegrees, 10000000 for the E7 of
    many GPS formats), moved in degrees, and an integer comes back an integer.
    """

    NAME = 'coordinate'
    OPTIONS = {'axis': _choiceOption('latitude', 'longitude'), 'meters': _integerOption(1, 1000000), 'latitudeColumn': _textOption,
               'scale': _integerOption(1, 10 ** 9)}
    REQUIRED = ('axis',)
    CACHEABLE = True
    CONTEXT_OPTION = 'latitudeColumn'

    @classmethod
    def checkOptions(cls, options: Dict[str, Any]) -> None:

        if 'latitudeColumn' in options and options['axis'] != 'longitude':
            raise ValueError('strategy "coordinate" takes latitudeColumn for a longitude only')


    def _degrees(self, message: bytes, latitude: Any) -> float:
        """The keyed move, in degrees along the axis, signed."""

        meters = self.options.get('meters', 1000) * (0.5 + 0.5 * self.keyedHash.unit(message, b'distance'))
        direction = 1 if self.keyedHash.unit(message, b'direction') < 0.5 else -1
        perDegree = _METRES_PER_DEGREE
        if latitude is not None and self.options['axis'] == 'longitude':
            # Near a pole a degree of longitude spans almost nothing; a
            # floor keeps the move from becoming the whole circle.
            perDegree *= max(0.01, math.cos(math.radians(float(latitude))))

        return direction * meters / perDegree


    def _moved(self, value: float, degrees: float) -> float:

        moved = value + degrees
        if self.options['axis'] == 'latitude':
            if abs(value) <= 90 < abs(moved):
                moved = value - degrees
            return moved

        return (moved + 180) % 360 - 180 if -180 <= value <= 180 else moved


    def _inDegrees(self, value: Any) -> float:
        """`value` in degrees, refused as a MaskingError, naming neither it nor
        its magnitude, where it is outside the axis's range.
        """

        scale = self.options.get('scale')
        degrees = float(value) / scale if scale else float(value)
        limit = _AXIS_LIMITS[self.options['axis']]
        if not -limit <= degrees <= limit:
            raise MaskingError('a {} is within {} degrees either side of zero, and a value here is not{}'.format(
                self.options['axis'], limit, ', even divided by its scale' if scale else
                '. If the column holds degrees multiplied by a power of ten -- microdegrees, say -- set scale'))

        return degrees


    def _move(self, value: Any, latitude: Any) -> Any:
        """`value` moved in degrees -- divided by `scale` first, if set -- and
        given back at its own type: a float as a float, a Decimal at its own
        scale, and a scaled integer as an integer, each of the last two moved
        at least one step. An integer that isn't scaled comes back a float.
        """

        if isinstance(value, bool) or not isinstance(value, (int, float, decimal.Decimal)):
            raise MaskingError('the coordinate strategy needs a number, got {}'.format(_typeName(value)))
        scale = self.options.get('scale')
        if isinstance(latitude, bool) or not isinstance(latitude, (int, float, decimal.Decimal)) or not math.isfinite(latitude):
            latitude = None
        elif scale:
            latitude = float(latitude) / scale

        if isinstance(value, (float, decimal.Decimal)) and not math.isfinite(value):
            # NaN and the infinities are no position, and give none away.
            return value

        degrees = self._degrees(canonical(value), latitude)
        moved = self._moved(self._inDegrees(value), degrees) * (scale or 1)
        if isinstance(value, float) or (isinstance(value, int) and not scale):
            return moved

        step = 1 if isinstance(value, int) else decimal.Decimal(1).scaleb(min(0, int(value.as_tuple().exponent)))
        result = round(moved) if isinstance(value, int) else decimal.Decimal(repr(moved)).quantize(step, rounding=decimal.ROUND_HALF_EVEN)
        if result == value:
            result = value + step if degrees > 0 else value - step

        return result


    def mask(self, value: Any) -> Any:

        return self._move(value, None)


    def maskColumnWith(self, values: Sequence[Any], context: Sequence[Any], chunkIndex: int) -> List[Any]:

        return [None if value is None else self._move(value, latitude) for value, latitude in zip(values, context)]


class IpStrategy(Strategy):
    """An IP address as another of its family, keyed and one-to-one: every
    IPv4 address to an IPv4 address and every IPv6 to an IPv6, by key's
    permutation over the 2**32 or 2**128 of them. No other strategy kept an
    address valid -- digits and key made `589.439.074.458`, and hash a hex
    token -- so a PostgreSQL inet column refused the copy.

    `keepPrefix` keeps that many leading bits, a /16 say, so the copy keeps
    which subnet an address was in; the rest are permuted with the kept bits
    as the permutation's tweak, so the same host in two subnets moves apart.

    Given as it came: text as text, PostgreSQL's address, interface (inet
    with a mask, whose prefix length is kept) and network (cidr, whose host
    bits stay zero) types as themselves, 4 or 16 bytes as bytes, and an
    integer below 2**32, as MySQL's INET_ATON stores one, as an integer.
    """

    NAME = 'ip'
    CACHEABLE = True
    OPTIONS = {'keepPrefix': _integerOption(0, 128)}

    def _permuted(self, number: int, bits: int) -> int:
        """`number`, of `bits` bits, with all but its first keepPrefix permuted."""

        keep = min(self.options.get('keepPrefix', 0), bits)
        free = bits - keep
        if free == 0:
            return number

        kept = number >> free
        low = number & ((1 << free) - 1)
        tweak = b'ip|' + bits.to_bytes(1, 'big') + b'|' + kept.to_bytes((keep + 7) // 8 or 1, 'big')

        return (kept << free) | self.keyedHash.permute(1 << free, low, tweak)


    def mask(self, value: Any) -> Any:

        # An interface first: it is a subclass of its address type.
        if isinstance(value, (ipaddress.IPv4Interface, ipaddress.IPv6Interface)):
            return ipaddress.ip_interface('{}/{}'.format(self._typed(value.ip), value.network.prefixlen))
        if isinstance(value, (ipaddress.IPv4Address, ipaddress.IPv6Address)):
            return self._typed(value)
        if isinstance(value, (ipaddress.IPv4Network, ipaddress.IPv6Network)):
            hostBits = value.max_prefixlen - value.prefixlen
            networkBits = int(value.network_address) >> hostBits
            masked = self._permuted(networkBits, value.prefixlen) << hostBits if value.prefixlen else 0
            return ipaddress.ip_network('{}/{}'.format(ipaddress.ip_address(masked) if value.version == 4 else ipaddress.IPv6Address(masked),
                                                       value.prefixlen))
        if isinstance(value, str):
            return str(self.mask(_readAddress(value.strip())))
        if isinstance(value, (bytes, bytearray, memoryview)) and len(value) in (4, 16):
            raw = bytes(value)
            return self._permuted(int.from_bytes(raw, 'big'), len(raw) * 8).to_bytes(len(raw), 'big')
        if isinstance(value, int) and not isinstance(value, bool) and 0 <= value < 1 << 32:
            return self._permuted(value, 32)

        raise MaskingError('the ip strategy needs an IP address -- as text, an address type, 4 or 16 bytes, or an integer below 2**32 -- '
                           'got {}'.format(_typeName(value)))


    def _typed(self, address: Any) -> Any:

        number = self._permuted(int(address), address.max_prefixlen)

        return ipaddress.IPv4Address(number) if address.version == 4 else ipaddress.IPv6Address(number)


def _readAddress(text: str) -> Any:
    """An address, an interface (with a /prefix) or, failing that, a network,
    read from text; a MaskingError naming neither the text nor its length.
    """

    for read in (ipaddress.ip_address, ipaddress.ip_interface, ipaddress.ip_network):
        try:
            return read(text)
        except ValueError:
            continue

    raise MaskingError('the ip strategy could not read a text value as an IP address')
