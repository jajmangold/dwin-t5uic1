# SPDX-License-Identifier: GPL-3.0-or-later
"""Encoder/button backends for the stock Ender 3 V2 front panel.

The panel's knob is passive: the rotary encoder and its push button are wired
straight through the ribbon to the host, not to the display controller. So
input has nothing to do with the UART protocol and everything to do with which
machine you are running on. Each backend below presents the same surface::

    position()    -> int    cumulative detents since construction
    take_delta()  -> int    detents since the last call, then resets to 0
    pressed()     -> bool   True once per physical press (edge consumed)
    close()

Panel connector, 10-way IDC ribbon:

    1 +5V   2 GND   3 encoder A   4 encoder B   5 beeper
    6 button   7 panel TX   8 panel RX   9 NC   10 NC

Pins 1 and 2 are the power pair. Identify them with a meter rather than
counting pins -- the notch-to-pin-1 convention is documented inconsistently
and 5 V on a signal line destroys the panel. See docs/wiring.md.

Encoder and button lines idle high and pull to ground, so every backend
enables pull-ups.
"""

__all__ = ['Quadrature', 'GpiozeroEncoder', 'EvdevEncoder']

_LAZY = {
    'Quadrature': '.quadrature',
    'GpiozeroEncoder': '.gpiozero',
    'EvdevEncoder': '.evdev',
}


def __getattr__(name):
    """Import backends on demand so one missing dependency is not fatal.

    This is PEP 562, which MicroPython does not implement, so ``input.Quadrature``
    resolves on CPython only. On a microcontroller import the submodule
    directly::

        from dwin_t5uic1.input.quadrature import Quadrature
    """
    module_name = _LAZY.get(name)
    if module_name is None:
        raise AttributeError(name)
    import importlib
    module = importlib.import_module(module_name, __name__)
    return getattr(module, name)
