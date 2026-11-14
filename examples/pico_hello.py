# SPDX-License-Identifier: GPL-3.0-or-later
"""Minimal MicroPython demo: text, shapes and the knob, on a Pico.

Copy dwin_t5uic1/ to the Pico (mpremote / Thonny / rshell) and run this as
main.py. Wiring (see docs/wiring.md):

    panel 1  (+5V)      <-  VBUS
    panel 2  (GND)      <-> GND
    panel 3  (enc A)    ->  GP2
    panel 4  (enc B)    ->  GP3
    panel 6  (button)   ->  GP4
    panel 7  (TX)       ->  GP1  (UART0 RX)
    panel 8  (RX)       <-  GP0  (UART0 TX)

Pins 1 and 2 are the power pair, at one end of the connector. Find them with a
meter rather than counting pins: the notch-to-pin-1 convention is documented
inconsistently, and 5 V on a signal line destroys the panel. Logic is 3.3 V on
both sides, so no level shifting.
"""
from dwin_t5uic1 import T5UIC1Display, rgb565
from dwin_t5uic1.input.quadrature import Quadrature
from dwin_t5uic1.transport import open_uart

AMBER = rgb565(255, 176, 0)
SLATE = rgb565(32, 36, 44)
WHITE = rgb565(255, 255, 255)


def main():
    panel = T5UIC1Display(open_uart(0, tx=0, rx=1))
    knob = Quadrature(a=2, b=3, button=4)

    panel.set_backlight(0xC0)
    panel.clear(SLATE)

    # The stock panel boots at 90 degrees: a 272 wide by 480 tall frame.
    panel.draw_rectangle(1, AMBER, 0, 0, panel.WIDTH - 1, 56)
    panel.draw_text(False, True, panel.FONT_16X32, SLATE, AMBER,
                    16, 12, 'HELLO PICO')
    panel.draw_text(False, True, panel.FONT_8X16, WHITE, SLATE,
                    16, 80, 'Turn the knob, press to zero')

    selection = 0
    while True:
        delta = knob.take_delta()
        if knob.pressed():
            selection = 0
        elif delta:
            selection += delta

        if delta or knob.pressed():
            # show_background=True repaints the cell, so no clearing needed.
            panel.draw_signed_scaled_float_text(
                panel.FONT_20X40, SLATE, 4, 0, 150, 140, selection)
            panel.draw_text(False, True, panel.FONT_8X16, WHITE, SLATE,
                            16, 200, 'last delta %+d  ' % delta)


if __name__ == '__main__':
    main()
