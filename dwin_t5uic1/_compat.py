# SPDX-License-Identifier: GPL-3.0-or-later
"""Runtime shims that let the driver run on CPython and MicroPython alike.

Nothing here is T5UIC1-specific. Each shim replaces a CPython-only facility
the original Petrel driver used (``threading``, ``concurrent.futures``,
``decimal``, ``unicodedata``, ``collections.deque``) with something a
microcontroller port can also provide.
"""

# --------------------------------------------------------------------------
# Monotonic clock
# --------------------------------------------------------------------------
try:                                              # CPython
    from time import monotonic
except ImportError:                               # MicroPython
    from time import ticks_ms, ticks_diff

    _ticks_last = ticks_ms()
    _ticks_accumulated = 0.0

    def monotonic():
        """Wrap-safe seconds since import.

        ``ticks_ms()`` wraps at a port-dependent limit, so accumulate signed
        differences instead of returning the raw counter.
        """
        global _ticks_last, _ticks_accumulated
        now = ticks_ms()
        _ticks_accumulated += ticks_diff(now, _ticks_last) / 1000.0
        _ticks_last = now
        return _ticks_accumulated

try:
    from time import sleep
except ImportError:                               # pragma: no cover
    from time import sleep_ms

    def sleep(seconds):
        sleep_ms(int(seconds * 1000))


# --------------------------------------------------------------------------
# Lock
# --------------------------------------------------------------------------
try:                                              # CPython
    from threading import Lock
except ImportError:                               # pragma: no cover
    try:                                          # MicroPython with _thread
        from _thread import allocate_lock as Lock
    except ImportError:                           # single-threaded port

        class Lock:
            """No-op context manager for ports without threads."""

            def __enter__(self):
                return self

            def __exit__(self, *exc_info):
                return False

            def acquire(self, *args):
                return True

            def release(self):
                pass


# --------------------------------------------------------------------------
# Float predicates (math.isfinite is absent on some MicroPython ports)
# --------------------------------------------------------------------------
_INF = float('inf')


def isfinite(value):
    return value == value and -_INF < value < _INF


def to_bytes_be(value, length, signed=False):
    """Big-endian encode, replacing ``int.to_bytes(..., signed=True)``.

    MicroPython's ``int.to_bytes`` accepts neither a ``signed`` keyword nor
    negative values, so the two's-complement fold is done here.
    """
    span = 1 << (length * 8)
    if signed:
        if not -(span >> 1) <= value <= (span >> 1) - 1:
            raise ValueError('numeric value does not fit requested byte length')
        if value < 0:
            value += span
    elif not 0 <= value < span:
        raise ValueError('numeric value does not fit requested byte length')
    return bytes((value >> (8 * (length - 1 - index))) & 0xFF
                 for index in range(length))


# --------------------------------------------------------------------------
# Bounded FIFO
# --------------------------------------------------------------------------
class Ring:
    """Bounded FIFO with the ``deque(maxlen=N)`` semantics the driver needs.

    MicroPython's ``collections.deque`` is too restricted to rely on (no
    predictable iteration or filtering), and the driver both iterates and
    rebuilds its queues, so keep an explicit list-backed ring.
    """

    __slots__ = ('_items', 'maxlen')

    def __init__(self, maxlen, items=()):
        if not isinstance(maxlen, int) or maxlen <= 0:
            raise ValueError('maxlen must be a positive integer')
        self.maxlen = maxlen
        self._items = []
        for item in items:
            self.append(item)

    def append(self, item):
        self._items.append(item)
        if len(self._items) > self.maxlen:
            del self._items[0]

    def popleft(self):
        return self._items.pop(0)

    def clear(self):
        del self._items[:]

    def __len__(self):
        return len(self._items)

    def __bool__(self):
        return bool(self._items)

    __nonzero__ = __bool__

    def __iter__(self):
        return iter(tuple(self._items))


# --------------------------------------------------------------------------
# Promise (subset of concurrent.futures.Future)
# --------------------------------------------------------------------------
class Promise:
    """The part of ``Future`` the Flash queue actually uses.

    Deliberately not thread-safe and never blocking: the owner polls
    ``T5UIC1Display.poll_flash_tasks`` and then inspects the promise.
    """

    __slots__ = ('_done', '_cancelled', '_result', '_error')

    def __init__(self):
        self._done = False
        self._cancelled = False
        self._result = None
        self._error = None

    def set_result(self, value):
        if self._done:
            raise RuntimeError('promise already settled')
        self._result = value
        self._done = True

    def set_exception(self, error):
        if self._done:
            raise RuntimeError('promise already settled')
        self._error = error
        self._done = True

    def done(self):
        return self._done

    def cancel(self):
        if self._done:
            return False
        self._cancelled = True
        self._done = True
        return True

    def cancelled(self):
        return self._cancelled

    def exception(self):
        return self._error

    def result(self):
        """Return the value, raise the error, or raise if still pending."""
        if not self._done:
            raise RuntimeError('promise is still pending; keep polling')
        if self._cancelled:
            raise RuntimeError('promise was cancelled')
        if self._error is not None:
            raise self._error
        return self._result


# --------------------------------------------------------------------------
# Panel text folding
# --------------------------------------------------------------------------
# The panel font library covers printable ASCII only. CPython reproduces
# Petrel's behaviour exactly via NFKD; MicroPython has no unicodedata, so it
# falls back to an explicit fold table covering Latin-1 and Turkish.
_EXPLICIT_FOLD = {
    'ı': 'i', 'İ': 'I', 'ş': 's', 'Ş': 'S',
    'ğ': 'g', 'Ğ': 'G',
}

# Only entries NFKD actually produces, so the two paths agree character for
# character. Characters NFKD leaves undecomposed must NOT be listed here:
# the degree sign, plus/minus, dashes, curly quotes and the ae/oe/o-slash/
# eszett letters all normalise to '?' on CPython, and listing a nicer
# substitute would make the same UI render differently on a microcontroller.
# An application that prefers 'o' for the degree sign should fold its own
# strings before handing them to the driver.
_FALLBACK_FOLD = {
    'à': 'a', 'á': 'a', 'â': 'a', 'ã': 'a',
    'ä': 'a', 'å': 'a', 'ç': 'c',
    'è': 'e', 'é': 'e', 'ê': 'e', 'ë': 'e',
    'ì': 'i', 'í': 'i', 'î': 'i', 'ï': 'i',
    'ñ': 'n', 'ò': 'o', 'ó': 'o', 'ô': 'o',
    'õ': 'o', 'ö': 'o', 'ù': 'u',
    'ú': 'u', 'û': 'u', 'ü': 'u', 'ý': 'y',
    'ÿ': 'y',
    'À': 'A', 'Á': 'A', 'Â': 'A', 'Ã': 'A',
    'Ä': 'A', 'Å': 'A', 'Ç': 'C',
    'È': 'E', 'É': 'E', 'Ê': 'E', 'Ë': 'E',
    'Ì': 'I', 'Í': 'I', 'Î': 'I', 'Ï': 'I',
    'Ñ': 'N', 'Ò': 'O', 'Ó': 'O', 'Ô': 'O',
    'Õ': 'O', 'Ö': 'O', 'Ù': 'U',
    'Ú': 'U', 'Û': 'U', 'Ü': 'U', 'Ý': 'Y',
    # Compatibility decompositions that expand to several characters.
    '…': '...',
}

try:
    import unicodedata as _unicodedata
except ImportError:                               # MicroPython
    _unicodedata = None


def panel_text(text):
    """Fold arbitrary text down to the printable ASCII the panel can render."""
    text = ''.join(_EXPLICIT_FOLD.get(char, char) for char in str(text))
    if _unicodedata is not None:
        text = _unicodedata.normalize('NFKD', text)
        return ''.join(char if ' ' <= char <= '~' else '?'
                       for char in text
                       if not _unicodedata.combining(char))
    result = []
    for raw in text:
        for char in _FALLBACK_FOLD.get(raw, raw):
            result.append(char if ' ' <= char <= '~' else '?')
    return ''.join(result)


# --------------------------------------------------------------------------
# Fixed-point formatting (replaces decimal.Decimal / ROUND_HALF_UP)
# --------------------------------------------------------------------------
def format_scaled(value, integer_digits, fractional_digits, signed):
    """Render a scaled integer as right-aligned fixed-point text.

    ``value`` is pre-scaled: ``(1234, integer_digits=2, fractional_digits=2)``
    renders as ``"12.34"``. Rounding is half-away-from-zero, matching
    ``ROUND_HALF_UP``; overflow renders as ``#`` padding, as the panel UI
    expects. Pure integer arithmetic, so there is no float drift and no
    ``decimal`` dependency.
    """
    limit = 10 ** (integer_digits + fractional_digits)
    width = (integer_digits
             + (fractional_digits + 1 if fractional_digits else 0)
             + (1 if signed else 0))

    if isinstance(value, int):
        number = value
    else:
        try:
            scalar = float(value)
        except (TypeError, ValueError) as error:
            raise ValueError('invalid numeric value') from error
        if not isfinite(scalar):
            raise ValueError('numeric value must be finite')
        if abs(scalar) >= limit:
            return '#' * width
        number = int(scalar + 0.5) if scalar >= 0 else -int(-scalar + 0.5)

    if abs(number) >= limit:
        return '#' * width

    digits = str(-number if number < 0 else number)
    if fractional_digits:
        if len(digits) <= fractional_digits:
            digits = digits.rjust(fractional_digits + 1, '0')
        text = digits[:-fractional_digits] + '.' + digits[-fractional_digits:]
    else:
        text = digits
    if number < 0:
        text = '-' + text
    return text.rjust(width) if len(text) <= width else '#' * width
