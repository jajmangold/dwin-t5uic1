# dwin_t5uic1

Portable Python driver for DWIN **T5UIC1** displays — the stock Ender 3 V2
screen — on anything with a spare UART. MicroPython (Pico, ESP32) and CPython
(Linux, Windows, macOS) run the same core.

The panel is a 480×272 LCD driven over a 115200 baud UART with a documented
instruction set. It stores its own fonts, JPEG pages and icon libraries in
flash, so the host sends drawing commands rather than pixels. That makes it
cheap to drive from a microcontroller: no framebuffer, no SPI bandwidth, a few
hundred bytes of RAM.

## Credit

This is a port. The protocol work, the length-aware framing, the Data Flash
state machine and the opcode reference all come from
**[Petrel](https://github.com/sezgynus/Petrel)** by sezgynus — a full Klipper
interface for this panel. If you want a finished printer UI on a Raspberry Pi,
use Petrel; this library only exists to run its driver layer somewhere Petrel
cannot go.

GPL-3.0-or-later, as Petrel is. `docs/t5uic1-reference.md` is Petrel's protocol
reference, included unchanged.

## Wiring

10-way IDC ribbon header on the back of the panel:

| Pin | Signal | Connect to | Stock colour |
|---|---|---|---|
| 1 | +5V | 5V, ~200–300 mA | black |
| 2 | GND | GND | white |
| 3 | Encoder A | GPIO, pull-up | grey |
| 4 | Encoder B | GPIO, pull-up | purple |
| 5 | Beeper | GPIO / PWM | blue |
| 6 | Encoder button | GPIO, pull-up | green |
| 7 | TX (display → host) | host UART RX | yellow |
| 8 | RX (host → display) | host UART TX | orange |
| 9, 10 | NC | — | red, brown |

**Pins 1 and 2 are the power pair, and they are at one end of the connector.**
Count from the wrong end and 5 V lands on a signal line, which destroys the
panel. The notch-to-pin-1 relationship is documented inconsistently, so
**identify the 5 V pair with a meter rather than counting pins** — the
procedure, and per-board maps for Pico, Pi and USB-serial, are in
[`docs/wiring.md`](docs/wiring.md).

Panel logic is 3.3 V, so a Pico or any 3.3 V MCU connects directly with no
level shifting.

The knob is passive: the encoder and its button are wired through the ribbon to
your MCU, not to the display controller. There is no touch panel.

## Install

Pure Python, no build step. Copy the `dwin_t5uic1/` directory next to your code
(or onto the Pico with `mpremote cp -r dwin_t5uic1 :`). On a host, `pip install
pyserial` for the serial backend; `python-evdev` only if you want the generic
Linux knob backend.

## Quick start

```python
from dwin_t5uic1 import T5UIC1Display, rgb565
from dwin_t5uic1.transport import open_uart      # or open_pyserial

panel = T5UIC1Display(open_uart(0, tx=0, rx=1))
panel.set_backlight(0xC0)
panel.clear(rgb565(24, 26, 32))
panel.draw_text(False, True, panel.FONT_16X32,
                rgb565(255, 176, 0), rgb565(24, 26, 32),
                16, 40, 'HELLO PICO')
```

On a host instead:

```python
from dwin_t5uic1.transport import open_pyserial
panel = T5UIC1Display(open_pyserial('/dev/ttyUSB0'))   # or 'COM5'
```

Runnable demos are in `examples/`: `pico_hello.py` (MicroPython, knob + text)
and `host_menu.py` (scrolling menu over USB-serial).

### Two things that trip people up

- **The frame is portrait.** A stock panel's `T5UIC1.CFG` boots at 90°, which
  presents the 480×272 LCD as `WIDTH = 272`, `HEIGHT = 480`. You do not need to
  call `set_orientation()`; it only matters if you are overriding the CFG.
- **`update()` (`0x3D`) is legacy.** Drawing commands take effect on their own.
  The method exists for compatibility with Creality/mriscoc-derived code.

## Opcode map

| Opcode | Function | Method |
|---:|---|---|
| `0x00` | Handshake | `handshake()`, `begin_liveness_probe()` / `poll_liveness_probe()` |
| `0x01` | Clear screen | `clear()` |
| `0x02` | Point / point list | `draw_point()`, `draw_points()` |
| `0x03` | Line / polyline | `draw_line()`, `draw_polyline()` |
| `0x05` | Rectangle / fill / XOR | `draw_rectangle()` |
| `0x08` | Two-colour bitmap | `draw_bitmap()` |
| `0x09` | Move display area | `move_area()` |
| `0x11` | Text | `draw_text()`, `draw_text_bytes()` |
| `0x14` | Native numeric variable | `draw_number()` |
| `0x21` | QR code | `draw_qr()` |
| `0x22` | Show JPEG page | `show_jpeg()` |
| `0x23` | Icon from library | `show_icon()` |
| `0x24` | JPEG from SRAM | `show_sram_jpeg()` |
| `0x25` | Cache JPEG to area 1 | `cache_jpeg()`, `ensure_cached_picture()` |
| `0x26` | Copy area 1 | `copy_cache1()` |
| `0x27` | Copy area 0/1 | `copy_cache()` |
| `0x28` | Icon animation | `configure_animation()` |
| `0x29` | Animation mask | `set_animation_mask()` |
| `0x2A` | EAN-13 barcode | `draw_ean13()` |
| `0x30` | Backlight | `set_backlight()` |
| `0x31` | SRAM/Flash write | `write_sram()`, `write_flash()`, `begin_flash_write_verified()` |
| `0x32` | SRAM/Flash read | `read_sram()`, `read_flash()`, `begin_flash_read()` |
| `0x33` | SRAM to Picture Flash | `store_sram_as_picture()` |
| `0x34` | Orientation | `set_orientation()` |
| `0x38` | Aux UART baud | `set_aux_baudrate()`, `set_aux_baud_divisor()` |
| `0x39` | Aux UART transmit | `aux_write()` |
| `0x3A` | Aux UART receive | `poll_aux_data()` |
| `0x3D` | Display update (legacy) | `update()` |
| `0xFF` | CRC error report | `poll_crc_error()` |

Software-drawn extras, since the panel has no primitive for them:
`draw_circle()`, `fill_circle()`, and the fixed-point text renderers
`draw_integer_text()`, `draw_scaled_float_text()`,
`draw_signed_scaled_float_text()`.

Fonts `FONT_6X12` … `FONT_32X64` (`0x00`–`0x09`) work with `0x11`;
`FONT_NUM_44X80` and `FONT_NUM_64X120` are numeric-only and valid for `0x14`.

## Data Flash

16 KiB of non-volatile panel memory, usable range `0x0000..0x3FFE` — the last
byte does not respond, which Petrel found by testing the physical panel.

`write_flash()`/`read_flash()` block. For a UI that must stay responsive, use
the queue instead: `begin_flash_write_verified(address, data)` returns a
promise, you call `poll_flash_tasks()` once per tick, and the driver does
write → ACK → readback verification without ever blocking you. A save only
succeeds on an exact readback; a lost ACK triggers bounded **read** retries and
never a blind rewrite.

## Transports and input

| Backend | Use | Needs |
|---|---|---|
| `transport.open_uart(id, tx, rx)` | MicroPython | `machine` |
| `transport.open_pyserial(port)` | any host | `pyserial` |
| `transport.LoopbackTransport()` | tests, packet preview | — |
| `input.Quadrature(a, b, button)` | MicroPython, IRQ-driven | `machine` |
| `input.GpiozeroEncoder(a, b, button)` | Raspberry Pi | `gpiozero`, `lgpio` |
| `input.EvdevEncoder(path)` | any Linux, incl. containers/VMs | `python-evdev` |

A transport is anything with `write`, `read`, `in_waiting` and `close` —
`serial.Serial` already qualifies, so you can pass one straight in. All
backends import lazily, so a missing dependency never breaks the others.

On MicroPython, import input backends from the submodule rather than the
package — `from dwin_t5uic1.input.quadrature import Quadrature`. The
convenience re-export relies on PEP 562 module `__getattr__`, which
MicroPython does not implement.

## What was not ported

- **Petrel's UI** (`ui/`, `features/`, `moonraker/`) — needs CPython, Pillow,
  `websocket-client` and a live Moonraker. Use Petrel for that.
- **`broker.py`** — Linux `AF_UNIX` UART multiplexing, only needed when two
  processes share one panel.
- **The icon-library packer** (`artwork/`) — a Pillow build-time tool. This
  library displays icons already in panel flash; it does not pack them.
- **`display_settings` / `buzzer` records** — Petrel's own `PETDSPL2` /
  `PETBZR02` formats. Build them on `read_flash`/`write_flash`; the layout is
  in `docs/t5uic1-reference.md`.

## Known divergence from Petrel

Text folding is identical on CPython (same NFKD path). On MicroPython there is
no `unicodedata`, so a fold table covers ASCII, Latin-1, the Turkish letters
and the ellipsis; outside that set CPython folds more aggressively and the
table yields `?`. `tests/test_parity.py` asserts the two agree across the
supported set, so the difference stays bounded and documented rather than
silent. Characters NFKD leaves alone — the degree sign, `±`, dashes, curly
quotes, `æ/ø/ß` — render as `?` on both, matching Petrel. Fold them yourself
if you want something nicer.

## Tests

```
python tests/run_all.py
```

No hardware needed. 41 tests: byte-level frames checked literally against the
instruction set, fragmented and noisy RX, the Flash state machine, parity of
the replaced CPython facilities against the originals (~96k formatter cases),
and portability guards that fail if a CPython-only import creeps into the core.
