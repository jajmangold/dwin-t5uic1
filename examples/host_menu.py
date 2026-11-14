# SPDX-License-Identifier: GPL-3.0-or-later
"""Scrolling menu on a host (Linux, Windows, macOS) over a USB-serial adapter.

    python examples/host_menu.py /dev/ttyUSB0
    python examples/host_menu.py COM5 --encoder /dev/input/by-id/usb-...-event-kbd

With no --encoder the menu advances on its own every second, which is enough
to prove the panel and wiring work before any knob is attached.
"""
import argparse
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from dwin_t5uic1 import T5UIC1Display, rgb565
from dwin_t5uic1.transport import open_pyserial

BACKGROUND = rgb565(24, 26, 32)
HIGHLIGHT = rgb565(255, 176, 0)
TEXT = rgb565(228, 232, 240)
MUTED = rgb565(128, 134, 148)

ITEMS = ['Move axis', 'Temperature', 'Fan speed', 'Bed mesh',
         'Z offset', 'Filament', 'Diagnostics', 'About']

ROW_HEIGHT = 36
HEADER_HEIGHT = 52


def draw_header(panel, title):
    panel.draw_rectangle(1, HIGHLIGHT, 0, 0, panel.WIDTH - 1, HEADER_HEIGHT)
    panel.draw_text(False, True, panel.FONT_12X24, BACKGROUND, HIGHLIGHT,
                    12, 14, title)


def draw_rows(panel, selected):
    for index, label in enumerate(ITEMS):
        top = HEADER_HEIGHT + 8 + index * ROW_HEIGHT
        if top + ROW_HEIGHT > panel.HEIGHT:
            break
        chosen = index == selected
        background = HIGHLIGHT if chosen else BACKGROUND
        foreground = BACKGROUND if chosen else TEXT
        panel.draw_rectangle(1, background, 8, top,
                             panel.WIDTH - 9, top + ROW_HEIGHT - 4)
        panel.draw_text(False, True, panel.FONT_10X20, foreground, background,
                        20, top + 6, label)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('port', help='serial device, e.g. /dev/ttyUSB0 or COM5')
    parser.add_argument('--baud', type=int, default=115200,
                        help='panel main UART rate (fixed in T5UIC1.CFG)')
    parser.add_argument('--encoder', help='evdev node for a rotary knob')
    parser.add_argument('--no-handshake', action='store_true',
                        help='skip the 0x00 handshake (for a panel already running)')
    arguments = parser.parse_args()

    transport = open_pyserial(arguments.port, baudrate=arguments.baud)
    panel = T5UIC1Display(transport, handshake=not arguments.no_handshake)

    knob = None
    if arguments.encoder:
        from dwin_t5uic1.input.evdev import EvdevEncoder
        knob = EvdevEncoder(arguments.encoder)
        print('encoder: %s' % knob.name)

    panel.set_backlight(0xC0)
    panel.clear(BACKGROUND)
    draw_header(panel, 'MENU')
    selected = 0
    draw_rows(panel, selected)
    panel.draw_text(False, True, panel.FONT_6X12, MUTED, BACKGROUND,
                    12, panel.HEIGHT - 20, 'dwin_t5uic1 demo')

    try:
        while True:
            previous = selected
            if knob is not None:
                delta = knob.take_delta()
                if knob.pressed():
                    print('selected: %s' % ITEMS[selected])
                selected = (selected + delta) % len(ITEMS)
            else:
                time.sleep(1.0)
                selected = (selected + 1) % len(ITEMS)
            if selected != previous:
                draw_rows(panel, selected)
            elif knob is not None:
                time.sleep(0.02)
    except KeyboardInterrupt:
        pass
    finally:
        if knob is not None:
            knob.close()
        panel.close()


if __name__ == '__main__':
    main()
