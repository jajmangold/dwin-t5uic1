# SPDX-License-Identifier: GPL-3.0-or-later
"""Interrupt-driven quadrature decoder for MicroPython (RP2040, ESP32).

The Ender 3 V2 knob is a detented EC11: one click is four quadrature counts,
so raw counts are divided down to detents. Decoding uses a transition table
rather than edge counting, which rejects the contact bounce that makes naive
decoders jump two positions on a single click.
"""

try:
    from machine import Pin
except ImportError:                               # pragma: no cover
    Pin = None

# Indexed by (previous << 2) | current, where each 2-bit state is (A << 1) | B.
# +1 / -1 are valid single-step transitions; 0 is no movement or an invalid
# double transition, which is discarded rather than guessed at.
_TRANSITIONS = (
    0, -1, +1, 0,
    +1, 0, 0, -1,
    -1, 0, 0, +1,
    0, +1, -1, 0,
)


class Quadrature:
    """Encoder plus button on three GPIOs, driven entirely from IRQs.

    ``a``, ``b`` and ``button`` are GPIO numbers (or pre-built ``Pin``
    objects). ``detent_counts`` is 4 for the stock knob; set it to 1 if you
    want every quadrature count, or 2 for a half-step encoder.
    """

    def __init__(self, a, b, button=None, detent_counts=4,
                 debounce_ms=5, callback=None):
        if Pin is None:                           # pragma: no cover
            raise RuntimeError('machine.Pin is unavailable; '
                               'this backend needs MicroPython')
        if not isinstance(detent_counts, int) or detent_counts < 1:
            raise ValueError('detent_counts must be a positive integer')

        self._a = a if isinstance(a, Pin) else Pin(a, Pin.IN, Pin.PULL_UP)
        self._b = b if isinstance(b, Pin) else Pin(b, Pin.IN, Pin.PULL_UP)
        self._detent_counts = detent_counts
        self._callback = callback

        self._counts = 0
        self._position = 0
        self._claimed = 0
        self._state = (self._a.value() << 1) | self._b.value()
        self._invalid = 0

        self._a.irq(self._rotated, Pin.IRQ_RISING | Pin.IRQ_FALLING)
        self._b.irq(self._rotated, Pin.IRQ_RISING | Pin.IRQ_FALLING)

        self._button = None
        self._presses = 0
        if button is not None:
            self._debounce_ms = debounce_ms
            self._last_press = 0
            self._button = (button if isinstance(button, Pin)
                            else Pin(button, Pin.IN, Pin.PULL_UP))
            self._button.irq(self._clicked, Pin.IRQ_FALLING)

    # -- IRQ handlers (keep allocation-free; they run in interrupt context) --

    def _rotated(self, _pin):
        state = (self._a.value() << 1) | self._b.value()
        previous = self._state
        if state == previous:
            return
        self._state = state
        step = _TRANSITIONS[(previous << 2) | state]
        if not step:
            # Both lines changed between samples: a skipped or bounced edge
            # whose direction cannot be recovered. Count it, never guess.
            self._invalid += 1
            return
        self._counts += step
        # Truncate toward zero, not floor: with floor division the first
        # count in the negative direction already reads as a whole detent,
        # so one click back would travel four menu rows.
        counts = self._counts
        position = (counts // self._detent_counts if counts >= 0
                    else -((-counts) // self._detent_counts))
        if position != self._position:
            self._position = position
            if self._callback is not None:
                self._callback(position)

    def _clicked(self, _pin):
        from time import ticks_ms, ticks_diff
        now = ticks_ms()
        if ticks_diff(now, self._last_press) < self._debounce_ms:
            return
        self._last_press = now
        self._presses += 1

    # -- public surface ----------------------------------------------

    def position(self):
        return self._position

    def take_delta(self):
        """Detents accumulated since the previous call."""
        position = self._position
        delta = position - self._claimed
        self._claimed = position
        return delta

    def pressed(self):
        """True once per physical press; the edge is consumed."""
        if self._presses:
            self._presses -= 1
            return True
        return False

    def diagnostics(self):
        return {'counts': self._counts, 'position': self._position,
                'invalid_transitions': self._invalid,
                'pending_presses': self._presses}

    def close(self):
        self._callback = None
        for pin in (self._a, self._b, self._button):
            if pin is not None:
                try:
                    pin.irq(None)
                except Exception:
                    pass
