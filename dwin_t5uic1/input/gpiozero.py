# SPDX-License-Identifier: GPL-3.0-or-later
#
# Derived from Petrel (petrel/hardware/encoder.py),
# Copyright (C) 2025-2026 sezgynus and the Petrel contributors.
# https://github.com/sezgynus/Petrel
#
# Changes: adapted to the dwin_t5uic1 input surface (take_delta/pressed),
# optional button support added, pin factory selection made overridable.
"""Raspberry Pi encoder backend built on gpiozero.

Uses the lgpio pin factory by default: RPi.GPIO's edge detection drops events
under load, which shows up as a knob that occasionally scrolls the wrong way.
"""

from gpiozero import Button, Device, RotaryEncoder
from gpiozero.pins.lgpio import LGPIOFactory

try:
    from threading import Lock
except ImportError:                               # pragma: no cover
    class Lock:
        def __enter__(self):
            return self

        def __exit__(self, *exc_info):
            return False


class GpiozeroEncoder:
    """Encoder plus optional button on Pi GPIOs (BCM numbering)."""

    def __init__(self, a, b, button=None, detent_counts=4, callback=None,
                 pin_factory=None, bounce_time=0.01):
        if Device.pin_factory is None:
            Device.pin_factory = pin_factory or LGPIOFactory()
        if not isinstance(detent_counts, int) or detent_counts < 1:
            raise ValueError('detent_counts must be a positive integer')

        self._detent_counts = detent_counts
        self._callback = callback
        self._lock = Lock()
        self._counts = 0
        self._position = 0
        self._claimed = 0
        self._positive_events = 0
        self._negative_events = 0
        self._presses = 0

        self._encoder = RotaryEncoder(a=a, b=b, max_steps=0)
        self._encoder.when_rotated = self._rotated

        self._button = None
        if button is not None:
            self._button = Button(button, pull_up=True, bounce_time=bounce_time)
            self._button.when_pressed = self._clicked

    def _rotated(self):
        with self._lock:
            counts = self._encoder.steps
            delta = counts - self._counts
            if not delta:
                return
            if delta > 0:
                self._positive_events += delta
            else:
                self._negative_events -= delta
            self._counts = counts
            position = (counts // self._detent_counts if counts >= 0
                        else -((-counts) // self._detent_counts))
            if position != self._position:
                self._position = position
                # The callback only enqueues input, preserving event ordering.
                if self._callback is not None:
                    self._callback(position)

    def _clicked(self):
        with self._lock:
            self._presses += 1

    def position(self):
        with self._lock:
            return self._position

    def take_delta(self):
        with self._lock:
            position = self._position
            delta = position - self._claimed
            self._claimed = position
            return delta

    def pressed(self):
        with self._lock:
            if self._presses:
                self._presses -= 1
                return True
            return False

    def diagnostics(self):
        with self._lock:
            return {'gpio_positive_steps': self._positive_events,
                    'gpio_negative_steps': self._negative_events,
                    'counts': self._counts, 'position': self._position,
                    'pending_presses': self._presses}

    def close(self):
        self._callback = None
        self._encoder.close()
        if self._button is not None:
            self._button.close()
