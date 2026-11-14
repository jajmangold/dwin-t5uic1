# SPDX-License-Identifier: GPL-3.0-or-later
#
# dwin_t5uic1 -- portable driver for DWIN T5UIC1 displays
# Derived from Petrel, Copyright (C) 2025-2026 sezgynus and the Petrel
# contributors (https://github.com/sezgynus/Petrel), GPL-3.0-or-later.
#
# This program is free software: you can redistribute it and/or modify it
# under the terms of the GNU General Public License as published by the Free
# Software Foundation, either version 3 of the License, or (at your option)
# any later version. See the LICENSE file for the full text.
"""Portable driver for DWIN T5UIC1 command-driven displays.

The stock Ender 3 V2 screen is a T5UIC1 panel: a 480x272 LCD driven over a
115200 baud UART with a documented instruction set. It holds its own fonts,
JPEG pages and icon libraries in flash, so the host sends drawing commands
rather than pixels. That makes it usable from anything with a spare UART.

Quick start on a Pico::

    from dwin_t5uic1 import T5UIC1Display, rgb565
    from dwin_t5uic1.transport import open_uart

    panel = T5UIC1Display(open_uart(0, tx=0, rx=1))
    panel.set_backlight(0x1F)
    panel.clear(0x0000)
    panel.draw_text(False, True, panel.FONT_16X32, rgb565(255, 180, 0),
                    0x0000, 20, 40, 'HELLO PICO')

Quick start on a host with a USB-serial adapter::

    from dwin_t5uic1 import T5UIC1Display
    from dwin_t5uic1.transport import open_pyserial

    panel = T5UIC1Display(open_pyserial('/dev/ttyUSB0'))

See ``docs/t5uic1-reference.md`` for the opcode table, the ``T5UIC1.CFG``
field map and the Data Flash layout.
"""

from .driver import (T5UIC1Display, T5UIC1ProtocolError, T5UIC1TimeoutError)
from .protocol import (HEADER, MAX_DATA_LENGTH, TAIL, aux_baud_divisor,
                       aux_frames, take_response)

__version__ = '0.1.0'

__all__ = [
    'T5UIC1Display', 'T5UIC1ProtocolError', 'T5UIC1TimeoutError',
    'HEADER', 'TAIL', 'MAX_DATA_LENGTH',
    'take_response', 'aux_frames', 'aux_baud_divisor',
    'rgb565', 'cfg_baud_divisor', '__version__',
]

# The panel's main UART rate is baked into T5UIC1.CFG at offsets 0x08..0x09
# as this divisor, which is a different clock from the runtime 0x38
# auxiliary-UART divisor in protocol.aux_baud_divisor.
_CFG_UART_CLOCK = 7833600


def rgb565(red, green, blue):
    """Pack 8-bit RGB into the panel's 16-bit colour format."""
    for name, value in (('red', red), ('green', green), ('blue', blue)):
        if not isinstance(value, int) or not 0 <= value <= 255:
            raise ValueError('%s must be 0..255' % name)
    return ((red & 0xF8) << 8) | ((green & 0xFC) << 3) | (blue >> 3)


def cfg_baud_divisor(baudrate):
    """Return the ``T5UIC1.CFG`` main-UART divisor and attainable rate.

    Writing this to CFG offsets 0x08..0x09 is a boot-time change made on the
    SD card, not something the runtime protocol can alter. 115200 gives
    divisor 68 (``0x0044``), which is the stock Ender 3 V2 setting.
    """
    if not isinstance(baudrate, int) or baudrate <= 0:
        raise ValueError('baudrate must be a positive integer')
    divisor = round(_CFG_UART_CLOCK / baudrate)
    if not 1 <= divisor <= 1023:
        raise ValueError('main UART baudrate is outside panel range')
    return divisor, _CFG_UART_CLOCK / divisor
