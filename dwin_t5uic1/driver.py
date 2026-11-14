# SPDX-License-Identifier: GPL-3.0-or-later
#
# Derived from Petrel (petrel/hardware/dwin/driver.py),
# Copyright (C) 2025-2026 sezgynus and the Petrel contributors.
# https://github.com/sezgynus/Petrel
#
# Changes from the original:
#   * transport is injected instead of opening pyserial directly
#   * no imports of UI, theme, artwork, settings or buzzer modules
#   * CPython-only facilities replaced by the shims in ._compat
#   * panel asset/settings synchronisation removed (it was Petrel policy)
#
# This program is free software: you can redistribute it and/or modify it
# under the terms of the GNU General Public License as published by the Free
# Software Foundation, either version 3 of the License, or (at your option)
# any later version. See the LICENSE file for the full text.
"""Portable UART driver for DWIN T5UIC1 instruction-set displays.

The class here is limited to panel and protocol concerns. UI layout and
rendering policy live above it. Public methods map onto the T5UIC1 v2.3
instruction set, plus the small set of compatibility text renderers that
Petrel uses in place of the native ``0x14`` numeric opcode.

A transport is any object providing::

    write(data) -> int        number of bytes written
    read(size)  -> bytes      up to ``size`` bytes, non-blocking-ish
    in_waiting                int attribute or property
    close()

``dwin_t5uic1.transport`` ships implementations for MicroPython ``machine.UART``,
pyserial and the Petrel Unix-socket broker.
"""

import math

from . import protocol as dwin_protocol
from ._compat import (Lock, Promise, Ring, format_scaled, isfinite, monotonic,
                      panel_text, sleep, to_bytes_be)


class T5UIC1ProtocolError(RuntimeError):
    """Raised when a panel response is malformed or does not match a request."""


class T5UIC1TimeoutError(Exception):
    """Raised when an expected T5UIC1 response is not received in time."""


def _noop_log(level, message):
    """Default logger: discard. Pass ``log=`` to the driver to observe."""


class _Frame:
    """A received non-auxiliary frame, tagged with its arrival time.

    Indexable and iterable so ``frame[0]`` and ``command, payload = frame``
    both work, matching the tuple subclass the original used (MicroPython
    cannot attach attributes to a tuple subclass).
    """

    __slots__ = ('command', 'payload', 'received_at')

    def __init__(self, command, payload, received_at):
        self.command = command
        self.payload = payload
        self.received_at = received_at

    def __getitem__(self, index):
        return (self.command, self.payload)[index]

    def __iter__(self):
        return iter((self.command, self.payload))

    def __len__(self):
        return 2


class _AuxFrame:
    """An auxiliary (0x3A) upload payload, tagged with its arrival time."""

    __slots__ = ('payload', 'received_at')

    def __init__(self, payload, received_at):
        self.payload = payload
        self.received_at = received_at


class T5UIC1Display:
    SUPPORTS_NONBLOCKING_LIVENESS = True
    SUPPORTS_NONBLOCKING_FLASH = True
    HEADER = b'\xAA'
    TAIL = b'\xCC\x33\xC3\x3C'

    # The stock Ender 3 V2 panel CFG boots at 90 degrees, which presents the
    # 480x272 physical LCD as a 272x480 portrait frame. Override via the
    # constructor if your CFG or set_orientation() call differs.
    WIDTH = 272
    HEIGHT = 480

    SRAM_SIZE = 32 * 1024
    FLASH_SIZE = 16 * 1024
    # Physical validation on the reference panel shows that address 0x3FFF
    # does not respond to 0x32 reads and causes writes spanning it to receive
    # no acknowledgement. Keep the documented 16 KiB size, but expose only
    # 0x0000..0x3FFE through the runtime Data Flash API.
    FLASH_USABLE_SIZE = FLASH_SIZE - 1
    MEMORY_SRAM = 0x5A
    MEMORY_FLASH = 0xA5
    MAX_DATA_LENGTH = 248
    MEMORY_WRITE_CHUNK = 128
    # Conservative read transactions. The T5UIC1 guide documents 0x01..0xF0
    # for opcode 0x32; the smaller chunk avoids the non-responsive 0x3FFF
    # boundary reported on the reference panel.
    MEMORY_READ_CHUNK = 0x3F

    FONT_SIZES = (
        (6, 12), (8, 16), (10, 20), (12, 24), (14, 28),
        (16, 32), (20, 40), (24, 48), (28, 56), (32, 64),
    )

    FONT_6X12 = 0x00
    FONT_8X16 = 0x01
    FONT_10X20 = 0x02
    FONT_12X24 = 0x03
    FONT_14X28 = 0x04
    FONT_16X32 = 0x05
    FONT_20X40 = 0x06
    FONT_24X48 = 0x07
    FONT_28X56 = 0x08
    FONT_32X64 = 0x09
    # Numeric-only faces, valid for the native 0x14 opcode only.
    FONT_NUM_64X120 = 0x0A
    FONT_NUM_44X80 = 0x0B

    # RGB565. Petrel's graphite theme overrides Color_Bg_Black with 0x2125;
    # this library defaults to true black and takes background_color instead.
    Color_White = 0xE75E
    Color_Yellow = 0xFF0F
    Color_Bg_Window = 0x2967
    Color_Bg_Blue = 0x2966
    Color_Bg_Black = 0x0000
    Color_Bg_Red = 0xF00F
    Popup_Text_Color = 0xE75E
    Line_Color = 0x39C8
    Rectangle_Color = 0x063B
    Percent_Color = 0x063B
    BarFill_Color = 0x39C8
    Select_Color = 0x19E9

    RX_FRAME_AGE = 5.0
    RX_FRAME_LIMIT = 64
    AUX_RX_LIMIT = 64

    def __init__(self, transport, handshake=True, handshake_timeout=1.0,
                 handshake_attempts=3, wake_delay=0.750, packet_observer=None,
                 log=None, background_color=None, width=None, height=None):
        if not isfinite(handshake_timeout) or handshake_timeout <= 0:
            raise ValueError('handshake timeout must be positive')
        if not isinstance(handshake_attempts, int) or handshake_attempts <= 0:
            raise ValueError('handshake attempts must be positive')
        if not isfinite(wake_delay) or wake_delay < 0:
            raise ValueError('wake delay must be nonnegative')

        self.serial = transport
        self._log = log if log is not None else _noop_log
        self._closed = False
        self._needs_update = True
        self._defer_updates = False
        self._rx = bytearray()
        self._frames = Ring(self.RX_FRAME_LIMIT)
        self._aux_rx = Ring(self.AUX_RX_LIMIT)
        self._rx_discarded_frames = 0
        self._rx_discarded_aux = 0
        self._crc_errors = 0
        self._diagnostic_counts = {}
        self._backlight_raw = None
        self._cached_picture1 = None
        self._expected_response = None
        self._liveness_deadline = None
        self._response_slots = {}
        self._transaction_lock = Lock()
        self._packet_observer = packet_observer
        self._flash_queue = Ring(8)
        self._flash_job = None
        self._flash_fault = None

        if width is not None:
            self.WIDTH = width
        if height is not None:
            self.HEIGHT = height
        if background_color is not None:
            self.Color_Bg_Black = background_color

        try:
            if packet_observer is not None:
                packet_observer.reset_panel()
            if handshake:
                if wake_delay:
                    sleep(wake_delay)
                for _ in range(handshake_attempts):
                    if self.handshake(handshake_timeout):
                        break
                else:
                    raise T5UIC1TimeoutError('DWIN handshake timed out')
            # The panel CFG already selects its orientation. Preserve the
            # power-on picture instead of reapplying direction via 0x34.
        except BaseException:
            self.close()
            raise

    def close(self):
        if not self._closed:
            self._closed = True
            try:
                self.serial.close()
            except Exception:
                pass
            if self._flash_job is not None or self._flash_queue:
                self._flash_abort(OSError('LCD disconnected during Flash transaction'))
            observer = self._packet_observer
            if observer is not None:
                observer.disconnect_panel()

    def __enter__(self):
        return self

    def __exit__(self, *exc_info):
        self.close()
        return False

    # ------------------------------------------------------------------
    # Field encoding
    # ------------------------------------------------------------------

    @staticmethod
    def _u8(value, name='value'):
        if not isinstance(value, int) or not 0 <= value <= 0xFF:
            raise ValueError('%s must be 0..255' % name)
        return value

    @staticmethod
    def _u16(value, name='value'):
        # Preserve the legacy UI contract for calculations that yield exact
        # integer-valued floats (for example 256.0), while rejecting
        # fractional coordinates the wire protocol cannot represent.
        if isinstance(value, float):
            if not isfinite(value) or value != int(value):
                raise ValueError('%s must be an integer in 0..65535' % name)
            value = int(value)
        if not isinstance(value, int) or not 0 <= value <= 0xFFFF:
            raise ValueError('%s must be 0..65535' % name)
        return value

    @classmethod
    def _word(cls, value, name='value'):
        return to_bytes_be(cls._u16(value, name), 2)

    @classmethod
    def _words(cls, *values):
        return b''.join(cls._word(value) for value in values)

    @staticmethod
    def _ensure_bytes(data, name='data'):
        if not isinstance(data, (bytes, bytearray, memoryview)):
            raise ValueError('%s must be bytes-like' % name)
        return bytes(data)

    # ------------------------------------------------------------------
    # Transmit and receive
    # ------------------------------------------------------------------

    def _send(self, command, payload=b''):
        if self._closed:
            raise RuntimeError('LCD is closed')
        command = self._u8(command, 'command')
        payload = self._ensure_bytes(payload)
        if len(payload) > self.MAX_DATA_LENGTH:
            raise ValueError('T5UIC1 data field exceeds 248 bytes')
        if command in (0x00, 0x31, 0x32, 0x33, 0x34):
            # A queued reply from an earlier transaction cannot confirm this send.
            self._frames = Ring(self.RX_FRAME_LIMIT,
                                (frame for frame in self._frames
                                 if frame[0] != command))
            self._response_slots.pop(command, None)
        frame = self.HEADER + bytes((command,)) + payload + self.TAIL
        try:
            written = self.serial.write(frame)
        except OSError:
            self.close()
            raise
        if written != len(frame):
            self.close()
            raise OSError('Incomplete LCD frame write')
        counts = self._diagnostic_counts
        counts['tx_frames'] = counts.get('tx_frames', 0) + 1
        counts['tx_bytes'] = counts.get('tx_bytes', 0) + len(frame)
        if command == 0x00:
            counts['handshake_requests'] = counts.get('handshake_requests', 0) + 1
        observer = self._packet_observer
        if observer is not None:
            try:
                observer.feed(frame)
            except Exception as error:
                # A developer observer must never interrupt the physical LCD.
                self._log('error', 'LCD packet observer failed: %s' % (error,))
        self._needs_update = True
        return frame

    def _read_available(self):
        if self._closed:
            raise RuntimeError('LCD is closed')
        try:
            waiting = int(getattr(self.serial, 'in_waiting', 0) or 0)
            if waiting:
                data = self.serial.read(min(waiting, 4096))
                self._rx.extend(data)
                counts = self._diagnostic_counts
                counts['rx_bytes'] = counts.get('rx_bytes', 0) + len(data)
                self._parse_rx()
        except OSError:
            self.close()
            raise

    def _parse_rx(self):
        while True:
            response = dwin_protocol.take_response(self._rx)
            if response is None:
                return
            command, payload = response
            now = monotonic()
            if command == 0x3A:
                if len(self._aux_rx) == self._aux_rx.maxlen:
                    self._rx_discarded_aux += 1
                self._aux_rx.append(_AuxFrame(payload[1:], now))
            elif command == 0xFF:
                self._crc_errors += 1
                counts = self._diagnostic_counts
                counts['crc_notifications'] = counts.get('crc_notifications', 0) + 1
            else:
                frame = _Frame(command, payload, now)
                expected = self._expected_response
                job = self._flash_job
                awaiting = job['awaiting'] if job is not None else None
                if awaiting is not None and command == awaiting[0]:
                    expected = awaiting
                if command == 0x00 and self._liveness_deadline is not None:
                    expected = 0x00, None
                if (expected is not None and command == expected[0]
                        and (expected[1] is None or expected[1](payload))):
                    # At most three active RPC kinds, one ACK slot per opcode.
                    slots = self._response_slots
                    if command not in slots and len(slots) < 3:
                        slots[command] = frame
                else:
                    if len(self._frames) == self._frames.maxlen:
                        self._rx_discarded_frames += 1
                    self._frames.append(frame)

    def _take_response(self, command, predicate=None):
        now = monotonic()
        slot = self._response_slots.pop(command, None)
        if slot is not None:
            if (now - slot.received_at <= self.RX_FRAME_AGE
                    and slot[0] == command
                    and (predicate is None or predicate(slot[1]))):
                return slot[1]
        matched = None
        for _ in range(len(self._frames)):
            frame = self._frames.popleft()
            if now - frame.received_at > self.RX_FRAME_AGE:
                self._rx_discarded_frames += 1
                continue
            op, payload = frame
            if matched is None and op == command and (predicate is None or predicate(payload)):
                matched = payload
                continue
            self._frames.append(frame)
        return matched

    def rx_discard_counts(self):
        """Dropped oldest unmatched replies and auxiliary frames under RX pressure."""
        return {'responses': self._rx_discarded_frames, 'aux': self._rx_discarded_aux}

    def _wait_response(self, command, timeout, predicate=None):
        if not isfinite(timeout) or timeout <= 0:
            raise ValueError('timeout must be positive')
        deadline = monotonic() + timeout
        self._expected_response = command, predicate
        try:
            while True:
                payload = self._take_response(command, predicate)
                if payload is not None:
                    return payload
                remaining = deadline - monotonic()
                if remaining <= 0:
                    counts = self._diagnostic_counts
                    counts['response_timeouts'] = counts.get('response_timeouts', 0) + 1
                    raise T5UIC1TimeoutError(
                        'Timed out waiting for opcode 0x%02X' % command)
                self._read_available()
                if not getattr(self.serial, 'in_waiting', 0):
                    sleep(min(0.005, remaining))
        finally:
            self._expected_response = None
            self._response_slots.pop(command, None)

    @staticmethod
    def _ok(payload):
        return payload.endswith(b'OK')

    # ------------------------------------------------------------------
    # Configuration and interface instructions
    # ------------------------------------------------------------------

    def handshake(self, timeout=1.0):
        """Opcode 0x00. True when the panel answers ``OK``."""
        with self._transaction_lock:
            self._send(0x00)
            try:
                payload = self._wait_response(0x00, timeout)
            except T5UIC1TimeoutError:
                return False
            return payload == b'OK'

    def begin_liveness_probe(self, timeout=0.5):
        """Send one handshake; never wait for its ACK on the UI owner."""
        if not isfinite(timeout) or timeout <= 0:
            raise ValueError('timeout must be positive')
        if self._liveness_deadline is not None:
            return
        with self._transaction_lock:
            # A response from a prior timed-out probe cannot confirm this one.
            self._frames = Ring(self.RX_FRAME_LIMIT,
                                (frame for frame in self._frames if frame[0] != 0x00))
            self._response_slots.pop(0x00, None)
            self._expected_response = 0x00, None
            self._send(0x00)
            self._liveness_deadline = monotonic() + timeout

    def poll_liveness_probe(self):
        """None: pending/no probe, True: ACK, False: invalid/expired ACK."""
        if self._liveness_deadline is None:
            return None
        with self._transaction_lock:
            self._read_available()
            payload = self._take_response(0x00)
            if payload is not None:
                self._liveness_deadline = None
                self._expected_response = None
                return payload == b'OK'
            if monotonic() >= self._liveness_deadline:
                self._liveness_deadline = None
                self._expected_response = None
                self._response_slots.pop(0x00, None)
                return False
        return None

    def diagnostics(self):
        counts = dict(self._diagnostic_counts)
        counts.update(self.rx_discard_counts())
        return counts

    def set_backlight(self, brightness):
        """Opcode 0x30. Raw luminance 0..255."""
        self._send(0x30, bytes((self._u8(brightness, 'brightness'),)))
        self._backlight_raw = brightness

    def set_orientation(self, direction, wait_ack=False, timeout=1.0):
        """Opcode 0x34. 0/1/2/3 = 0/90/180/270 degrees.

        Only needed when overriding the orientation baked into T5UIC1.CFG.
        """
        if direction not in (0, 1, 2, 3):
            raise ValueError('direction must be 0..3')
        with self._transaction_lock:
            self._send(0x34, b'\x5A\xA5' + bytes((direction,)))
            if wait_ack:
                payload = self._wait_response(0x34, timeout, self._ok)
                if payload != b'OK':
                    raise T5UIC1ProtocolError('Invalid orientation acknowledgement')

    def set_aux_baud_divisor(self, divisor):
        """Opcode 0x38. Extended/auxiliary UART divisor, 1..0x03FF."""
        if not isinstance(divisor, int) or not 1 <= divisor <= 0x03FF:
            raise ValueError('extended UART divisor must be 1..0x03FF')
        self._send(0x38, self._word(divisor))

    def set_aux_baudrate(self, baudrate):
        """Set the auxiliary UART rate; returns the attainable rate."""
        divisor, actual = dwin_protocol.aux_baud_divisor(baudrate)
        self.set_aux_baud_divisor(divisor)
        return actual

    def aux_write(self, data):
        """Opcode 0x39. Transmit on the panel's auxiliary UART."""
        data = self._ensure_bytes(data)
        if not data:
            raise ValueError('extended UART write must not be empty')
        self._send(0x39, data)

    def poll_aux_data(self):
        """Opcode 0x3A. Next auxiliary upload payload, or None."""
        self._read_available()
        now = monotonic()
        while self._aux_rx:
            frame = self._aux_rx.popleft()
            if now - frame.received_at <= self.RX_FRAME_AGE:
                return bytes(frame.payload)
            self._rx_discarded_aux += 1
        return None

    def poll_crc_error(self):
        """Opcode 0xFF. True once per inbound CRC-error report."""
        self._read_available()
        if not self._crc_errors:
            return False
        self._crc_errors -= 1
        return True

    def update(self):
        """Opcode 0x3D.

        Legacy on the T5UIC1: drawing commands take effect without it. Kept
        for compatibility with Creality/mriscoc-derived call sites.
        """
        if self._closed:
            raise RuntimeError('LCD is closed')
        if self._defer_updates or not self._needs_update:
            return
        self._send(0x3D)
        self._needs_update = False

    # ------------------------------------------------------------------
    # Drawing instructions
    # ------------------------------------------------------------------

    def clear(self, color):
        """Opcode 0x01."""
        self._send(0x01, self._word(color, 'color'))

    def draw_points(self, color, width, height, points):
        """Opcode 0x02. ``points`` is an iterable of (x, y) pairs."""
        color = self._u16(color, 'color')
        if not isinstance(width, int) or not 1 <= width <= 15:
            raise ValueError('point width must be 1..15')
        if not isinstance(height, int) or not 1 <= height <= 15:
            raise ValueError('point height must be 1..15')
        points = tuple(points)
        if not points:
            raise ValueError('at least one point is required')
        payload = self._word(color) + bytes((width, height))
        for point in points:
            if not isinstance(point, (tuple, list)) or len(point) != 2:
                raise ValueError('points must contain (x, y) pairs')
            payload += self._words(point[0], point[1])
        self._send(0x02, payload)

    def draw_point(self, color, width, height, x, y):
        self.draw_points(color, width, height, ((x, y),))

    def draw_polyline(self, color, points):
        """Opcode 0x03."""
        color = self._u16(color, 'color')
        points = tuple(points)
        if len(points) < 2:
            raise ValueError('a polyline needs at least two points')
        payload = self._word(color)
        for point in points:
            if not isinstance(point, (tuple, list)) or len(point) != 2:
                raise ValueError('points must contain (x, y) pairs')
            payload += self._words(point[0], point[1])
        self._send(0x03, payload)

    def draw_line(self, color, x0, y0, x1, y1):
        self.draw_polyline(color, ((x0, y0), (x1, y1)))

    def draw_rectangle(self, mode, color, x0, y0, x1, y1):
        """Opcode 0x05. mode 0 = outline, 1 = fill, 2 = XOR."""
        if mode not in (0, 1, 2):
            raise ValueError('rectangle mode must be 0, 1 or 2')
        payload = (bytes((mode,)) + self._word(color, 'color')
                   + self._words(x0, y0, x1, y1))
        self._send(0x05, payload)

    def draw_bitmap(self, x, y, width, color1, color0, data):
        """Opcode 0x08. Two-colour bitmap, rows left-aligned and byte-padded."""
        if not isinstance(width, int) or not 1 <= width <= 480:
            raise ValueError('bitmap width must be 1..480')
        data = self._ensure_bytes(data)
        row_bytes = (width + 7) // 8
        if not data or len(data) % row_bytes:
            raise ValueError('bitmap data must contain complete left-aligned rows')
        payload = (self._words(x, y) + self._word(width)
                   + self._word(color1, 'color1')
                   + self._word(color0, 'color0') + data)
        self._send(0x08, payload)

    def move_area(self, mode, direction, distance, color, x0, y0, x1, y1):
        """Opcode 0x09. mode 0 = fill vacated area, 1 = translate."""
        if mode not in (0, 1):
            raise ValueError('move mode must be 0 or 1')
        if direction not in (0, 1, 2, 3):
            raise ValueError('move direction must be 0..3')
        flags = (mode << 7) | direction
        payload = bytes((flags,)) + self._words(distance, color, x0, y0, x1, y1)
        self._send(0x09, payload)

    # Software-only helpers: the panel has no circle primitive.
    def draw_circle(self, color, x0, y0, radius):
        if not isinstance(radius, int) or radius < 0:
            raise ValueError('radius must be a nonnegative integer')
        x = radius
        y = 0
        error = 1 - radius
        while x >= y:
            points = (
                (x0 + x, y0 + y), (x0 + y, y0 + x),
                (x0 - y, y0 + x), (x0 - x, y0 + y),
                (x0 - x, y0 - y), (x0 - y, y0 - x),
                (x0 + y, y0 - x), (x0 + x, y0 - y),
            )
            self.draw_points(color, 1, 1, points)
            y += 1
            if error < 0:
                error += 2 * y + 1
            else:
                x -= 1
                error += 2 * (y - x) + 1

    def fill_circle(self, color, x0, y0, radius):
        if not isinstance(radius, int) or radius < 0:
            raise ValueError('radius must be a nonnegative integer')
        for dy in range(-radius, radius + 1):
            dx = int(math.sqrt(radius * radius - dy * dy))
            self.draw_rectangle(1, color, x0 - dx, y0 + dy, x0 + dx, y0 + dy)

    # ------------------------------------------------------------------
    # Text and numeric instructions
    # ------------------------------------------------------------------

    _panel_text = staticmethod(panel_text)

    def draw_text_bytes(self, width_adjust, show_background, font, color,
                        background, x, y, data):
        """Opcode 0x11 with a pre-encoded ASCII payload."""
        if not isinstance(font, int) or not 0 <= font <= 9:
            raise ValueError('font must be 0..9')
        data = self._ensure_bytes(data)
        if not data:
            return
        mode = ((1 if width_adjust else 0) << 7
                | (1 if show_background else 0) << 6
                | font)
        payload = (bytes((mode,)) + self._word(color, 'color')
                   + self._word(background, 'background')
                   + self._words(x, y) + data)
        self._send(0x11, payload)

    def draw_text(self, width_adjust, show_background, font, color,
                  background, x, y, text):
        """Opcode 0x11. Text is folded to ASCII and clipped to the frame."""
        x = self._u16(x, 'x')
        y = self._u16(y, 'y')
        if not isinstance(font, int) or not 0 <= font <= 9:
            raise ValueError('font must be 0..9')
        font_width = self.FONT_SIZES[font][0]
        visible = min(90, max(0, (self.WIDTH - x) // font_width))
        encoded = panel_text(text)[:visible].encode('ascii')
        if encoded and y < self.HEIGHT:
            self.draw_text_bytes(width_adjust, show_background, font, color,
                                 background, x, y, encoded)

    def draw_number(self, value, signed=False, show_background=False,
                    zero_fill=False, zero_mode=False, font=FONT_8X16,
                    color=0xFFFF, background=0, integer_digits=1,
                    fractional_digits=0, x=0, y=0, byte_length=4):
        """Opcode 0x14, the panel's native numeric variable renderer."""
        if not isinstance(font, int) or not 0 <= font <= 0x0F:
            raise ValueError('native numeric font selector must be 0x00..0x0F')
        if not isinstance(integer_digits, int) or not 1 <= integer_digits <= 20:
            raise ValueError('integer_digits must be 1..20')
        if not isinstance(fractional_digits, int) or not 0 <= fractional_digits <= 20:
            raise ValueError('fractional_digits must be 0..20')
        if integer_digits + fractional_digits > 20:
            raise ValueError('integer_digits + fractional_digits must not exceed 20')
        if not isinstance(byte_length, int) or not 1 <= byte_length <= 8:
            raise ValueError('byte_length must be 1..8')
        if not isinstance(value, int):
            raise ValueError('native numeric value must be an integer')
        raw = to_bytes_be(value, byte_length, bool(signed))
        mode = ((1 if show_background else 0) << 7
                | (1 if signed else 0) << 6
                | (1 if zero_fill else 0) << 5
                | (1 if zero_mode else 0) << 4
                | font)
        payload = (bytes((mode,)) + self._word(color, 'color')
                   + self._word(background, 'background')
                   + bytes((integer_digits, fractional_digits))
                   + self._words(x, y) + raw)
        self._send(0x14, payload)

    def _format_scaled_decimal(self, value, integer_digits, fractional_digits, signed):
        return format_scaled(value, integer_digits, fractional_digits, signed)

    def draw_integer_text(self, show_background, zero_fill, zero_mode, font,
                          color, background, digits, x, y, value):
        """Render an integer as 0x11 text rather than a native 0x14 variable."""
        if not isinstance(digits, int) or not 1 <= digits <= 19:
            raise ValueError('digits must be 1..19')
        text = format_scaled(value, digits, 0, False)
        self.draw_text(False, show_background, font, color, background, x, y, text)

    def draw_scaled_float_text(self, show_background, zero_fill, zero_mode, font,
                               color, background, integer_digits, fractional_digits,
                               x, y, value):
        """Render a pre-scaled fixed-point value as 0x11 text."""
        if not (isinstance(integer_digits, int) and isinstance(fractional_digits, int)
                and 1 <= integer_digits <= 19 and 0 <= fractional_digits <= 18
                and integer_digits + fractional_digits < 20):
            raise ValueError('invalid numeric digit count')
        font_width = self.FONT_SIZES[font][0] if isinstance(font, int) and 0 <= font <= 9 else 0
        text = format_scaled(value, integer_digits, fractional_digits, True)
        self.draw_text(False, show_background, font, color, background,
                       max(0, x - font_width), y, text)

    def draw_signed_scaled_float_text(self, font, background, integer_digits,
                                      fractional_digits, x, y, value):
        self.draw_scaled_float_text(
            True, True, False, font, self.Color_White, background,
            integer_digits, fractional_digits, x, y, value,
        )

    # ------------------------------------------------------------------
    # Picture, icon, QR and barcode instructions
    # ------------------------------------------------------------------

    @staticmethod
    def _image_mode(index, background, restore, enhanced):
        if not isinstance(index, int) or not 0 <= index <= 0x0F:
            raise ValueError('image library/cache index must be 0..15')
        return ((1 if background else 0) << 7
                | (1 if restore else 0) << 6
                | (1 if enhanced else 0) << 5
                | index)

    def draw_qr(self, x, y, pixel_size, data):
        """Opcode 0x21. Panel-rendered QR code, 1..154 payload bytes."""
        if not isinstance(pixel_size, int) or not 1 <= pixel_size <= 15:
            raise ValueError('QR pixel size must be 1..15')
        if isinstance(data, str):
            data = data.encode('utf-8')
        data = self._ensure_bytes(data)
        if not 1 <= len(data) <= 154:
            raise ValueError('QR data must contain 1..154 bytes')
        self._send(0x21, self._words(x, y) + bytes((pixel_size,)) + data)

    def show_jpeg(self, jpeg_id):
        """Opcode 0x22. Display a stored JPEG page and cache it to area 0."""
        if not isinstance(jpeg_id, int) or not 0 <= jpeg_id <= 15:
            raise ValueError('JPEG ID must be 0..15')
        self._send(0x22, bytes((0, jpeg_id)))

    def show_icon(self, library_id, icon_ids, x, y, background=True,
                  restore=False, enhanced=False):
        """Opcode 0x23. One or more icons from a panel ICO library."""
        mode = self._image_mode(library_id, background, restore, enhanced)
        if isinstance(icon_ids, int):
            icon_ids = (icon_ids,)
        icon_ids = tuple(icon_ids)
        if not icon_ids:
            raise ValueError('at least one icon ID is required')
        icons = bytes(self._u8(icon, 'icon ID') for icon in icon_ids)
        self._send(0x23, self._words(x, y) + bytes((mode,)) + icons)

    def show_sram_jpeg(self, x, y, address=0, background=True, enhanced=False):
        """Opcode 0x24. Decode a JPEG already uploaded into panel SRAM."""
        if not isinstance(address, int) or not 0 <= address < self.SRAM_SIZE:
            raise ValueError('SRAM address outside 32KB range')
        mode = ((1 if background else 0) << 7) | ((1 if enhanced else 0) << 5)
        self._send(0x24, self._words(x, y) + bytes((mode,)) + self._word(address))

    def cache_jpeg(self, jpeg_id, area=1):
        """Opcode 0x25. Decode a JPEG into virtual area 1 without displaying."""
        if not isinstance(jpeg_id, int) or not 0 <= jpeg_id <= 15:
            raise ValueError('JPEG ID must be 0..15')
        if area != 1:
            raise ValueError('cache-only JPEG loading supports virtual area 1; '
                             'use show_jpeg for area 0')
        self._send(0x25, bytes((area, jpeg_id)))
        self._cached_picture1 = jpeg_id

    def ensure_cached_picture(self, jpeg_id):
        """Reuse Area 1 until another picture or connection replaces it."""
        if self._cached_picture1 != jpeg_id:
            self.cache_jpeg(jpeg_id)

    def copy_cache1(self, x0, y0, x1, y1, x, y):
        """Opcode 0x26. Blit a region of virtual area 1 to the screen."""
        self._send(0x26, self._words(x0, y0, x1, y1, x, y))

    def copy_cache(self, cache_id, x0, y0, x1, y1, x, y,
                   background=False, restore=False, enhanced=True):
        """Opcode 0x27. Blit a region of virtual area 0 or 1."""
        if cache_id not in (0, 1):
            raise ValueError('virtual display area must be 0 or 1')
        mode = self._image_mode(cache_id, background, restore, enhanced)
        self._send(0x27, bytes((mode,)) + self._words(x0, y0, x1, y1, x, y))

    def configure_animation(self, animation_id, enabled, library_id,
                            first_icon, last_icon, x, y, interval,
                            restart_from_first=True):
        """Opcode 0x28. Panel-driven icon animation; interval is 10 ms units."""
        if not isinstance(animation_id, int) or not 0 <= animation_id <= 15:
            raise ValueError('animation ID must be 0..15')
        if not isinstance(library_id, int) or not 0 <= library_id <= 15:
            raise ValueError('icon library ID must be 0..15')
        first_icon = self._u8(first_icon, 'first icon')
        last_icon = self._u8(last_icon, 'last icon')
        interval = self._u8(interval, 'interval')
        mode = ((1 if enabled else 0) << 7
                | (1 if restart_from_first else 0) << 6
                | animation_id)
        payload = (self._words(x, y)
                   + bytes((mode, library_id, first_icon, last_icon, interval)))
        self._send(0x28, payload)

    def set_animation_mask(self, state):
        """Opcode 0x29. Bitmask of which animation channels run."""
        self._send(0x29, self._word(state, 'animation mask'))

    def draw_ean13(self, x, y, digits):
        """Opcode 0x2A. EAN-13 barcode from 12 digits; coordinates must be even."""
        if isinstance(digits, str):
            if len(digits) != 12 or not digits.isdigit():
                raise ValueError('EAN-13 source must contain exactly 12 digits')
            digits = tuple(int(char) for char in digits)
        else:
            digits = tuple(digits)
        if len(digits) != 12 or any(not isinstance(value, int) or not 0 <= value <= 9
                                    for value in digits):
            raise ValueError('EAN-13 source must contain exactly 12 digits '
                             'in the range 0..9')
        if x % 2 or y % 2:
            raise ValueError('EAN-13 coordinates must be even')
        self._send(0x2A, self._words(x, y) + bytes(digits))

    # ------------------------------------------------------------------
    # SRAM, Data Flash and Picture Flash instructions
    # ------------------------------------------------------------------

    @classmethod
    def _memory_limit(cls, memory):
        if memory == cls.MEMORY_SRAM:
            return cls.SRAM_SIZE
        if memory == cls.MEMORY_FLASH:
            return cls.FLASH_USABLE_SIZE
        raise ValueError('memory must be MEMORY_SRAM or MEMORY_FLASH')

    def write_memory(self, memory, address, data, timeout=1.25):
        """Opcode 0x31. Blocking; Data Flash writes wait for each ACK."""
        limit = self._memory_limit(memory)
        if not isinstance(address, int):
            raise ValueError('memory address must be an integer')
        data = self._ensure_bytes(data)
        if not data:
            raise ValueError('memory write must not be empty')
        if not 0 <= address < limit or address + len(data) > limit:
            raise ValueError('memory write outside selected memory range')

        with self._transaction_lock:
            offset = 0
            while offset < len(data):
                chunk = data[offset:offset + self.MEMORY_WRITE_CHUNK]
                self._send(0x31, bytes((memory,))
                           + self._word(address + offset) + chunk)
                if memory == self.MEMORY_FLASH:
                    expected = bytes((self.MEMORY_FLASH,)) + b'OK'
                    payload = self._wait_response(
                        0x31, timeout, lambda value: value == expected)
                    if payload != expected:
                        raise T5UIC1ProtocolError('Invalid Flash write acknowledgement')
                offset += len(chunk)

    def read_memory(self, memory, address, length, timeout=1.0):
        """Opcode 0x32. Blocking, chunked, response matched to the request."""
        limit = self._memory_limit(memory)
        if not isinstance(address, int) or not isinstance(length, int):
            raise ValueError('memory address and length must be integers')
        if length <= 0 or not 0 <= address < limit or address + length > limit:
            raise ValueError('memory read outside selected memory range')

        result = bytearray()
        with self._transaction_lock:
            offset = 0
            while offset < length:
                size = min(self.MEMORY_READ_CHUNK, length - offset)
                chunk_address = address + offset
                self._send(0x32, bytes((memory,))
                           + self._word(chunk_address) + bytes((size,)))

                def matches(payload, size=size, chunk_address=chunk_address):
                    return (len(payload) == 4 + size
                            and payload[0] == memory
                            and int.from_bytes(payload[1:3], 'big') == chunk_address
                            and payload[3] == size)

                payload = self._wait_response(0x32, timeout, matches)
                if not matches(payload):
                    raise T5UIC1ProtocolError('Memory response does not match request')
                result.extend(payload[4:])
                offset += size
        return bytes(result)

    def write_sram(self, address, data):
        self.write_memory(self.MEMORY_SRAM, address, data)

    def read_sram(self, address, length, timeout=1.0):
        return self.read_memory(self.MEMORY_SRAM, address, length, timeout=timeout)

    def write_flash(self, address, data, timeout=1.25):
        self.write_memory(self.MEMORY_FLASH, address, data, timeout=timeout)

    def read_flash(self, address, length, timeout=1.0):
        return self.read_memory(self.MEMORY_FLASH, address, length, timeout=timeout)

    # -- Non-blocking Flash queue --------------------------------------

    def _flash_submit(self, address, value, verify):
        limit = self.FLASH_USABLE_SIZE
        size = len(value) if verify else value
        if (not isinstance(address, int) or not isinstance(size, int)
                or size <= 0 or not 0 <= address < limit
                or address + size > limit):
            raise ValueError('Flash transaction exceeds usable memory')
        promise = Promise()
        if self._closed:
            promise.set_exception(OSError('LCD disconnected'))
        elif len(self._flash_queue) + (1 if self._flash_job is not None else 0) >= 4:
            promise.set_exception(RuntimeError('Flash request queue full'))
        else:
            # A failed transaction invalidates its queued followers, but must
            # not permanently disable all subsequent saves. A new request is
            # an explicit retry after the previous queue was failed and drained.
            if self._flash_fault is not None:
                self._log('warning', 'Retrying DWIN Flash after failure: %s'
                          % (self._flash_fault,))
                self._flash_fault = None
                self._expected_response = None
                for op in (0x31, 0x32):
                    self._response_slots.pop(op, None)
                self._frames = Ring(self.RX_FRAME_LIMIT,
                                    (frame for frame in self._frames
                                     if frame[0] not in (0x31, 0x32)))
            self._flash_queue.append({
                'address': address, 'length': size,
                'data': value if verify else None, 'future': promise,
                'stage': 'write' if verify else 'read', 'offset': 0,
                'buffer': bytearray(), 'awaiting': None, 'deadline': 0.0,
                # T5UIC1 Data Flash may take up to 1s *per write*. On a repeat
                # write the panel can spend additional time internally
                # programming the same address. Keep the owner nonblocking,
                # but allow the delayed ACK rather than aborting at 1.25s.
                'chunk_size': 0, 'timeout': 3.0 if verify else 2.0,
                'verification_retries': 0, 'resume_at': 0.0,
            })
        return promise

    def begin_flash_read(self, address, length):
        """Schedule a Flash read and return a promise; the owner must poll."""
        return self._flash_submit(address, length, False)

    def begin_flash_write_verified(self, address, data):
        """Schedule write, ACK and readback verification without blocking."""
        return self._flash_submit(address, self._ensure_bytes(data), True)

    def flash_busy(self):
        return bool(self._flash_job or self._flash_queue)

    def _flash_abort(self, error):
        self._flash_fault = error
        self._expected_response = None
        for op in (0x31, 0x32):
            self._response_slots.pop(op, None)
        jobs = [self._flash_job] if self._flash_job is not None else []
        jobs.extend(self._flash_queue)
        self._flash_job = None
        self._flash_queue.clear()
        for job in jobs:
            promise = job['future']
            if not promise.done():
                promise.set_exception(error)

    def _flash_retry_readback(self, job, reason):
        """Reconcile a possibly completed write by *reading*, never rewriting.

        T5UIC1 can finish Data Flash programming even if the host missed the
        0x31 ACK, or its first 0x32 response. In those cases reporting
        'not saved' would be false. The only acceptable success is an exact
        readback of the bytes requested. A mismatch or unreadable value gets
        bounded read retries.
        """
        if job['data'] is None or job['verification_retries'] >= 4:
            return False
        job['verification_retries'] += 1
        job['stage'] = 'read'
        job['offset'] = 0
        job['chunk_size'] = 0
        job['buffer'] = bytearray()
        job['awaiting'] = None
        self._expected_response = None
        # A delayed ACK or read reply is not evidence for any subsequent
        # request; clear only Flash RPCs, retaining liveness and AUX traffic.
        for op in (0x31, 0x32):
            self._response_slots.pop(op, None)
        self._frames = Ring(self.RX_FRAME_LIMIT,
                            (frame for frame in self._frames
                             if frame[0] not in (0x31, 0x32)))
        job['resume_at'] = monotonic() + 0.15 * job['verification_retries']
        self._log('warning',
                  'DWIN Flash confirmation retry %d/4: address=0x%04X '
                  'cause=%s (read-only)'
                  % (job['verification_retries'], job['address'], reason))
        return True

    def poll_flash_tasks(self):
        """Advance at most one Flash packet/ACK, with no sleep or ACK spin.

        The caller owns all UART access. On error, cancel the current
        transaction and its queued followers; a subsequent explicit request
        can retry without restarting the panel or the application.
        """
        if self._flash_fault is not None:
            return False
        if self._flash_job is None:
            if not self._flash_queue:
                return False
            self._flash_job = self._flash_queue.popleft()
        job = self._flash_job
        if job['future'].cancelled():
            self._flash_abort(RuntimeError('Cancelled Flash transaction'))
            return False
        try:
            if job['awaiting'] is not None:
                self._read_available()
                op, predicate = job['awaiting']
                matched = self._take_response(op, predicate)
                if matched is None:
                    if monotonic() >= job['deadline']:
                        if self._flash_retry_readback(
                                job, 'missing 0x%02X response' % op):
                            return True
                        raise T5UIC1TimeoutError(
                            'Flash confirmation timed out at 0x%02X' % op)
                    return True
                job['awaiting'] = None
                self._expected_response = None
                job['offset'] += job['chunk_size']
                if job['stage'] == 'read':
                    job['buffer'].extend(matched[4:])
                if job['offset'] >= job['length']:
                    if job['stage'] == 'write':
                        job['stage'], job['offset'] = 'read', 0
                    else:
                        result = bytes(job['buffer'])
                        if job['data'] is not None and result != job['data']:
                            if self._flash_retry_readback(job, 'readback mismatch'):
                                return True
                            raise T5UIC1ProtocolError(
                                'Flash verification mismatch after read retries')
                        if job['data'] is not None and job['verification_retries']:
                            self._log('info',
                                      'DWIN Flash save verified by readback after '
                                      '%d confirmation retries: address=0x%04X'
                                      % (job['verification_retries'], job['address']))
                        job['future'].set_result(result)
                        self._flash_job = None
                return True

            # Send only one packet per tick. Readback retries have a short
            # nonblocking settle period; do not issue another write.
            if monotonic() < job.get('resume_at', 0):
                return True
            remaining = job['length'] - job['offset']
            address = job['address'] + job['offset']
            if job['stage'] == 'write':
                chunk = job['data'][job['offset']:
                                    job['offset'] + self.MEMORY_WRITE_CHUNK]
                expected = bytes((self.MEMORY_FLASH,)) + b'OK'
                op = 0x31
                payload = bytes((self.MEMORY_FLASH,)) + self._word(address) + chunk
                predicate = lambda value: value == expected
                size = len(chunk)
            else:
                size = min(self.MEMORY_READ_CHUNK, remaining)
                op = 0x32
                payload = (bytes((self.MEMORY_FLASH,))
                           + self._word(address) + bytes((size,)))

                def predicate(value, size=size, address=address):
                    return (len(value) == 4 + size
                            and value[0] == self.MEMORY_FLASH
                            and int.from_bytes(value[1:3], 'big') == address
                            and value[3] == size)

            self._frames = Ring(self.RX_FRAME_LIMIT,
                                (frame for frame in self._frames if frame[0] != op))
            self._response_slots.pop(op, None)
            self._expected_response = op, predicate
            self._send(op, payload)
            job['chunk_size'] = size
            job['awaiting'] = op, predicate
            job['deadline'] = monotonic() + job['timeout']
            return True
        except (OSError, RuntimeError, ValueError, T5UIC1TimeoutError,
                T5UIC1ProtocolError) as error:
            self._log('warning',
                      'DWIN Flash RPC failed: address=0x%04X stage=%s offset=%d '
                      'opcode=%s: %s'
                      % (job['address'], job['stage'], job['offset'],
                         '0x%02X' % job['awaiting'][0] if job['awaiting'] else 'send',
                         error))
            self._flash_abort(error)
            # I/O errors still reach the caller's reconnect handler.
            if self._closed:
                raise
            return False

    def store_sram_as_picture(self, picture_id, timeout=2.5):
        """Opcode 0x33. Copy panel SRAM into a Picture Flash slot."""
        if not isinstance(picture_id, int) or not 0 <= picture_id <= 15:
            raise ValueError('picture ID must be 0..15')
        if self._cached_picture1 == picture_id:
            self._cached_picture1 = None
        with self._transaction_lock:
            self._send(0x33, b'\x5A\xA5' + bytes((picture_id,)))
            payload = self._wait_response(0x33, timeout, self._ok)
            if payload != b'OK':
                raise T5UIC1ProtocolError('Invalid picture Flash acknowledgement')
