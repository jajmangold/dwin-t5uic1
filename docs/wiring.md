# Pin map — Ender 3 V2 / T5UIC1 display connector

The panel's own connector: a 10-way IDC ribbon header on the back of the
display PCB.

```
 pin  signal              direction        stock ribbon colour
 ───────────────────────────────────────────────────────────────
  1   +5V                 power in         black
  2   GND                 power in         white
  3   Encoder A           out to host      grey
  4   Encoder B           out to host      purple
  5   Beeper              in from host     blue
  6   Encoder button      out to host      green
  7   TX   display ──▶ host                yellow
  8   RX   display ◀── host                orange
  9   NC   (not connected)                 red
 10   NC   (not connected)                 brown
```

Cross-checked against three independent sources that agree pin for pin:
the [SKR E3 Turbo thread](https://github.com/bigtreetech/BIGTREETECH-SKR-E3-Turbo/issues/40),
[mriscoc/Ender3V2S1 #462](https://github.com/mriscoc/Ender3V2S1/issues/462)
(where an off-the-shelf adapter had TX/RX in the wrong positions and had to be
moved back to 7/8), and community cable documentation. Relative order is also
consistent with Marlin's `pins_CREALITY_V4.h`, which puts `BTN_ENC`,
`BEEPER_PIN`, `BTN_EN2` and `BTN_EN1` on adjacent EXP3 pins.

## Read this before applying power

**Pin 1 is the +5V rail and pin 2 is ground.** Both sit at one end of the
connector, next to each other — which means that if you count from the wrong
end, 5 V lands on the encoder or serial lines. That kills the panel, and can
take your MCU with it.

I could not verify from primary sources how the notch on the display-side
housing relates to pin 1; published descriptions disagree, and the same
connector gets numbered from both ends in different write-ups. So **do not
count pins. Identify the power pair, then work outward:**

1. With the ribbon unplugged from the display, power the printer mainboard (or
   whatever was driving the panel) and measure the cable's contacts with a
   meter. The pair reading a steady 5 V is pins 1 and 2; the positive one is
   pin 1.
2. If you have the stock Creality ribbon, the **black** wire is pin 1 and the
   **white** wire is pin 2. Confirm with the meter anyway — Creality changed
   cables across production runs, and colour is a convenience, not a contract.
3. Nothing is on pins 9 and 10, so the two conductors at the far end from the
   power pair should read open. That is a useful second confirmation you have
   the orientation right.

Only then connect anything.

## Host side

Serial crosses over: host TX goes to the panel's **RX (pin 8)**, host RX comes
from the panel's **TX (pin 7)**.

The knob is passive. The encoder and its button are wired straight through the
ribbon to your host — the display controller does not read them and does not
report them over the UART. There is no touch panel. Encoder and button lines
idle high and pull to ground, so enable pull-ups on all three.

Panel logic is 3.3 V, so a Pico, ESP32 or any 3.3 V MCU connects directly with
no level shifting. The panel needs 5 V for its own supply and backlight, at
roughly 200–300 mA.

### Raspberry Pi Pico

```
 panel 1  (+5V)      ──  VBUS (pin 40)
 panel 2  (GND)      ──  GND  (pin 38)
 panel 3  (Encoder A)──  GP2, pull-up
 panel 4  (Encoder B)──  GP3, pull-up
 panel 5  (Beeper)   ──  GP5, PWM (optional)
 panel 6  (Button)   ──  GP4, pull-up
 panel 7  (TX)       ──  GP1  (UART0 RX)
 panel 8  (RX)       ──  GP0  (UART0 TX)
```

```python
from dwin_t5uic1 import T5UIC1Display
from dwin_t5uic1.input.quadrature import Quadrature
from dwin_t5uic1.transport import open_uart

panel = T5UIC1Display(open_uart(0, tx=0, rx=1))
knob = Quadrature(a=2, b=3, button=4)
```

### Raspberry Pi (Petrel's default assignment)

Reusing Petrel's defaults means a wiring harness works with either project.
BCM numbering:

```
 panel 1  (+5V)      ──  5V
 panel 2  (GND)      ──  GND
 panel 3  (Encoder A)──  GPIO21
 panel 4  (Encoder B)──  GPIO16
 panel 5  (Beeper)   ──  GPIO19 / physical 35, via a driver
 panel 6  (Button)   ──  GPIO20
 panel 7  (TX)       ──  GPIO15 / UART RX
 panel 8  (RX)       ──  GPIO14 / UART TX
```

```python
from dwin_t5uic1.input.gpiozero import GpiozeroEncoder
knob = GpiozeroEncoder(a=21, b=16, button=20)
```

Enable the serial hardware and disable the serial login console with
`sudo raspi-config`, then confirm which device maps to GPIO14/15 on your Pi
and OS before using it.

### Host with a USB-serial adapter

Any 3.3 V USB-UART (CP2102, FT232, CH340 on its 3.3 V setting) drives the
panel: adapter TX to panel pin 8, adapter RX to panel pin 7, adapter GND to
pin 2, and 5 V from the adapter's VBUS or a separate supply to pin 1. Do not
feed the panel 5 V from a 3.3 V-only regulator on the adapter; the backlight
draws more than those supply.

This is the route for a machine with no GPIO header — an x86 box, a VM, or a
container. The knob then needs its own source; see `EvdevEncoder`, which reads
any USB knob the kernel already exposes under `/dev/input`.

## Serial settings

115200 baud, 8N1, no flow control. The rate is fixed in `T5UIC1.CFG` at
offsets `0x08..0x09` as a divisor of 7,833,600 — stock is `0x0044` (68), which
gives 115200. Changing it is an SD-card operation on the panel, not something
the runtime protocol can do. See `t5uic1-reference.md`.
