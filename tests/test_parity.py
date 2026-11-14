# SPDX-License-Identifier: GPL-3.0-or-later
"""Parity tests: the portable shims must match Petrel's CPython originals.

``format_scaled`` replaces a ``decimal.Decimal`` implementation and
``panel_text`` replaces a ``unicodedata`` one. Both are exercised against
reimplementations of the originals so a behavioural drift fails loudly.
"""
import sys
import unicodedata
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from dwin_t5uic1._compat import format_scaled, panel_text, to_bytes_be


def petrel_format(value, integer_digits, fractional_digits, signed):
    """Verbatim logic of Petrel driver.py::_format_scaled_decimal."""
    try:
        number = Decimal(value) if isinstance(value, int) else Decimal(str(value))
        if not number.is_finite():
            raise ValueError('numeric value must be finite')
        limit = Decimal(10) ** (integer_digits + fractional_digits)
        if abs(number) < limit:
            number = number.quantize(Decimal(1), rounding=ROUND_HALF_UP)
        if number == 0:
            number = abs(number)
        width = integer_digits + (fractional_digits + 1 if fractional_digits else 0) + int(signed)
        if abs(number) >= limit:
            return '#' * width
        text = format(number.scaleb(-fractional_digits), '.%df' % fractional_digits)
        return text.rjust(width) if len(text) <= width else '#' * width
    except InvalidOperation as error:
        raise ValueError('invalid numeric value') from error


def petrel_panel_text(text):
    """Verbatim logic of Petrel driver.py::_panel_text."""
    text = str(text).translate(str.maketrans({
        'ı': 'i', 'İ': 'I', 'ş': 's', 'Ş': 'S', 'ğ': 'g', 'Ğ': 'G',
    }))
    text = unicodedata.normalize('NFKD', text)
    return ''.join(
        char if ' ' <= char <= '~' else '?'
        for char in text if not unicodedata.combining(char)
    )


def test_format_scaled_matches_decimal():
    checked = 0
    for integer_digits in range(1, 6):
        for fractional_digits in range(0, 4):
            if integer_digits + fractional_digits > 19:
                continue
            for signed in (False, True):
                values = list(range(-1200, 1200))
                values += [10 ** (integer_digits + fractional_digits) - 1,
                           10 ** (integer_digits + fractional_digits),
                           10 ** (integer_digits + fractional_digits) + 1,
                           -10 ** (integer_digits + fractional_digits)]
                for value in values:
                    mine = format_scaled(value, integer_digits, fractional_digits, signed)
                    theirs = petrel_format(value, integer_digits, fractional_digits, signed)
                    assert mine == theirs, (
                        value, integer_digits, fractional_digits, signed, mine, theirs)
                    checked += 1
    print('format_scaled: %d integer cases match' % checked)


def test_format_scaled_matches_decimal_for_floats():
    checked = 0
    for value in (0.0, 0.4, 0.5, 0.6, 1.5, 2.5, -0.4, -0.5, -0.6, -1.5, -2.5,
                  12.34, -12.34, 99.5, 100.49, 999.5, 1234.567, -1234.567):
        for integer_digits in range(1, 5):
            for fractional_digits in range(0, 3):
                for signed in (False, True):
                    mine = format_scaled(value, integer_digits, fractional_digits, signed)
                    theirs = petrel_format(value, integer_digits, fractional_digits, signed)
                    assert mine == theirs, (
                        value, integer_digits, fractional_digits, signed, mine, theirs)
                    checked += 1
    print('format_scaled: %d float cases match' % checked)


def test_panel_text_matches_unicodedata():
    samples = [
        'HELLO PICO', 'Nozzle 215C / Bed 60C',
        'Düşük ısı', 'ĞÜŞİÖÇ ğüşiöç',
        'café naïve', '45°C ± 2', 'em—dash – en', 'quote “x” and ‘y’',
        'Ellipsis…', 'tab\there', 'ctrl\x01char', '', 'Ünïcödé Mîx',
    ]
    for sample in samples:
        mine = panel_text(sample)
        theirs = petrel_panel_text(sample)
        assert mine == theirs, (sample, mine, theirs)
        assert all(' ' <= char <= '~' for char in mine), mine
    print('panel_text: %d samples match' % len(samples))


def test_fallback_fold_matches_nfkd_over_supported_set():
    """The MicroPython table path must agree with NFKD, not merely approximate it.

    Parity is guaranteed over the documented set: ASCII, Latin-1, the Turkish
    letters and the ellipsis. Outside it CPython folds more aggressively and
    the table yields '?', which is the safe direction -- a missing glyph, never
    a wrong one.
    """
    from dwin_t5uic1 import _compat

    supported = [chr(code) for code in range(0x20, 0x7F)]
    supported += [chr(code) for code in range(0xA0, 0x100)]
    supported += list('ışğİŞĞ')
    supported += ['…']

    saved = _compat._unicodedata
    _compat._unicodedata = None                 # force the table path
    try:
        fallback = {char: _compat.panel_text(char) for char in supported}
    finally:
        _compat._unicodedata = saved

    divergent = []
    for char in supported:
        reference = petrel_panel_text(char)
        produced = fallback[char]
        if produced != reference:
            # Only tolerated when the table declined to guess.
            if set(produced) != {'?'}:
                divergent.append((hex(ord(char)), reference, produced))
    assert not divergent, divergent

    # Spot-check the cases that motivated trimming the table.
    _compat._unicodedata = None
    try:
        assert _compat.panel_text('45°C') == '45?C'
        assert _compat.panel_text('café') == 'cafe'
        assert _compat.panel_text('Düşük ısı') == 'Dusuk isi'
        assert _compat.panel_text('wait…') == 'wait...'
    finally:
        _compat._unicodedata = saved
    print('panel_text: table path agrees with NFKD over %d supported chars'
          % len(supported))


def test_to_bytes_be_matches_int_to_bytes():
    checked = 0
    for length in range(1, 9):
        span = 1 << (length * 8)
        values = [0, 1, span - 1, span >> 1, (span >> 1) - 1]
        signed_values = [0, 1, -1, (span >> 1) - 1, -(span >> 1)]
        for value in values:
            assert to_bytes_be(value, length) == value.to_bytes(length, 'big')
            checked += 1
        for value in signed_values:
            assert to_bytes_be(value, length, True) == value.to_bytes(length, 'big', signed=True)
            checked += 1
    print('to_bytes_be: %d cases match' % checked)


def test_overflow_rejected():
    for bad in (256, -1):
        try:
            to_bytes_be(bad, 1)
        except ValueError:
            pass
        else:
            raise AssertionError('expected ValueError for %r' % bad)
    print('to_bytes_be: overflow rejected')


if __name__ == '__main__':
    for name, function in sorted(globals().items()):
        if name.startswith('test_'):
            function()
    print('\nALL PARITY TESTS PASSED')
