# SPDX-License-Identifier: GPL-3.0-or-later
"""Transport backends for :class:`dwin_t5uic1.T5UIC1Display`.

A transport is any object exposing this surface, which is the subset of the
pyserial API the driver actually touches::

    write(data) -> int     bytes written; the driver treats a short write as fatal
    read(size)  -> bytes    up to ``size`` bytes; may return b'' when idle
    in_waiting              int attribute or property; 0 when nothing is buffered
    close()                 idempotent

``serial.Serial`` already satisfies it, so pyserial objects can be passed
straight to the driver. The helpers here just construct one with the panel's
settings applied, and :class:`LoopbackTransport` provides a hardware-free
double for tests.

Importing this package pulls in no hardware dependency; each backend imports
its own requirements lazily.
"""

from ._loopback import LoopbackTransport

__all__ = ['LoopbackTransport', 'open_pyserial', 'open_uart', 'UARTTransport']


def open_pyserial(port, baudrate=115200, timeout=0.05, write_timeout=1.0):
    """Open a panel on a host serial port (Linux, Windows, macOS).

    ``port`` is a device name such as ``/dev/ttyAMA0``, ``/dev/ttyUSB0`` or
    ``COM5``. The panel's main UART rate is fixed in ``T5UIC1.CFG`` and is
    115200 on a stock Ender 3 V2 screen.
    """
    import serial
    return serial.Serial(port, baudrate=baudrate, timeout=timeout,
                         write_timeout=write_timeout)


def open_uart(uart_id, tx, rx, baudrate=115200, timeout=50, timeout_char=5):
    """Open a panel on a MicroPython ``machine.UART`` (Pico, ESP32).

    ``tx``/``rx`` are GPIO numbers. Serial crosses over: the host's TX goes to
    the panel's **RX, pin 8**, and the host's RX comes from the panel's
    **TX, pin 7**. Panel logic is 3.3 V, so no level shifting is needed.

    Pins 3 and 4 are Encoder A and B, not serial -- see docs/wiring.md for the
    full map and for why this pinout is mis-documented so widely (the board
    and panel ends of the ribbon are numbered in opposite directions).
    """
    from machine import UART, Pin
    uart = UART(uart_id, baudrate=baudrate, tx=Pin(tx), rx=Pin(rx),
                timeout=timeout, timeout_char=timeout_char)
    return UARTTransport(uart)


class UARTTransport:
    """Adapter presenting ``machine.UART`` through the transport surface."""

    __slots__ = ('_uart',)

    def __init__(self, uart):
        self._uart = uart

    @property
    def in_waiting(self):
        return self._uart.any()

    def read(self, size):
        return self._uart.read(size) or b''

    def write(self, data):
        written = self._uart.write(data)
        # Blocking MicroPython UARTs return None on success.
        return len(data) if written is None else written

    def close(self):
        deinit = getattr(self._uart, 'deinit', None)
        if deinit is not None:
            deinit()
