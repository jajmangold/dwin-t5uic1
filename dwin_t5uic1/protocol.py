# SPDX-License-Identifier: GPL-3.0-or-later
# Vendored verbatim from Petrel (petrel/hardware/dwin/protocol.py),
# Copyright (C) 2025-2026 sezgynus and the Petrel contributors.
# https://github.com/sezgynus/Petrel  --  GPL-3.0-or-later

"""Length-aware T5UIC1 framing shared by the UI and auxiliary UART broker.

Payloads may contain AA or the complete trailer. Never split on delimiters
inside a payload: memory replies and auxiliary uploads carry explicit lengths.
"""

HEADER = b'\xaa'
TAIL = b'\xcc\x33\xc3\x3c'
MAX_DATA_LENGTH = 248
FIXED_RX_PAYLOAD = {0x00: 2, 0x31: 3, 0x33: 2, 0x34: 2, 0xff: 1}


def aux_frames(data):
    """Frame binary TX without copying the unconsumed tail on every chunk."""
    offset = 0
    while offset < len(data):
        end = min(offset + MAX_DATA_LENGTH, len(data))
        marker = data.find(TAIL, offset, end)
        if marker >= 0:
            end = marker + len(TAIL) - 1
        yield b'\xaa\x39' + data[offset:end] + TAIL
        offset = end


def take_response(buffer):
    """Remove one complete valid inbound frame, or return None for partial RX.

    Mutates the caller-owned bytearray, preserving incomplete frames. Discards
    noise/unknown opcodes and resynchronizes after a malformed trailer.
    """
    # NOTE: MicroPython's bytearray implements neither item/slice deletion nor
    # .clear() - only CPython does. Both raise at runtime, so every RX path
    # here is dead on a Pico unless the buffer is trimmed by slice assignment,
    # which both runtimes support. Do not "simplify" these back to del.
    while True:
        start = buffer.find(HEADER)
        if start < 0:
            buffer[:] = b''
            return None
        if start:
            buffer[:] = buffer[start:]
        if len(buffer) < 2:
            return None
        command = buffer[1]
        if command == 0x32:
            if len(buffer) < 6:
                return None
            size = 4 + buffer[5]
        elif command == 0x3a:
            if len(buffer) < 3:
                return None
            size = 1 + buffer[2]
        else:
            size = FIXED_RX_PAYLOAD.get(command)
            if size is None:
                buffer[:] = buffer[1:]
                continue
        total = 2 + size + len(TAIL)
        if len(buffer) < total:
            return None
        if buffer[total - len(TAIL):total] != TAIL:
            buffer[:] = buffer[1:]
            continue
        payload = bytes(buffer[2:2 + size])
        buffer[:] = buffer[total:]
        return command, payload


def aux_baud_divisor(baudrate):
    """Return panel divisor and the actual attainable baud rate."""
    if not isinstance(baudrate, int) or baudrate < 15300:
        raise ValueError('extended UART baudrate must be at least 15300')
    divisor = round(15667200 / baudrate)
    if not 1 <= divisor <= 0x03ff:
        raise ValueError('extended UART baudrate is outside panel range')
    return divisor, 15667200 / divisor
