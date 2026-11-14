# SPDX-License-Identifier: GPL-3.0-or-later
"""Byte-level tests against the T5UIC1 v2.3 instruction set.

Every expected frame here is written out literally, so a refactor that
changes the wire format fails rather than silently drawing nothing on real
hardware.
"""
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from dwin_t5uic1 import (T5UIC1Display, T5UIC1ProtocolError, T5UIC1TimeoutError,
                         aux_baud_divisor, aux_frames, cfg_baud_divisor,
                         rgb565, take_response)
from dwin_t5uic1.transport import LoopbackTransport

TAIL = b'\xCC\x33\xC3\x3C'


def panel(auto_handshake=False, **kwargs):
    """Build a driver on a loopback transport, skipping the wake delay."""
    transport = LoopbackTransport(auto_handshake=auto_handshake)
    display = T5UIC1Display(transport, handshake=auto_handshake,
                            wake_delay=0, **kwargs)
    transport.clear()
    return display, transport


def frame(command, payload=b''):
    return b'\xAA' + bytes((command,)) + bytes(payload) + TAIL


# ----------------------------------------------------------------------
# Handshake and liveness
# ----------------------------------------------------------------------

def test_handshake_frame_and_response():
    transport = LoopbackTransport(auto_handshake=True)
    display = T5UIC1Display(transport, wake_delay=0)
    assert transport.frames[0] == b'\xAA\x00\xCC\x33\xC3\x3C'
    assert display.handshake() is True
    display.close()
    assert transport.closed
    print('handshake: request and OK response verified')


def test_handshake_failure_raises():
    transport = LoopbackTransport(auto_handshake=False)
    try:
        T5UIC1Display(transport, wake_delay=0, handshake_timeout=0.01,
                      handshake_attempts=2)
    except T5UIC1TimeoutError:
        assert transport.closed, 'a failed handshake must close the transport'
    else:
        raise AssertionError('expected T5UIC1TimeoutError')
    print('handshake: timeout closes transport')


def test_liveness_probe_is_nonblocking():
    display, transport = panel()
    display.begin_liveness_probe(timeout=5)
    assert transport.last_frame() == frame(0x00)
    assert display.poll_liveness_probe() is None, 'must not block for the ACK'
    transport.reply(0x00, b'OK')
    assert display.poll_liveness_probe() is True
    assert display.poll_liveness_probe() is None, 'probe is consumed'
    print('liveness: probe is nonblocking and single-shot')


# ----------------------------------------------------------------------
# Drawing opcodes
# ----------------------------------------------------------------------

def test_drawing_opcodes():
    display, transport = panel()
    checks = [
        (lambda: display.clear(0x1234), frame(0x01, b'\x12\x34')),
        (lambda: display.draw_point(0xFFFF, 1, 1, 10, 20),
         frame(0x02, b'\xFF\xFF\x01\x01\x00\x0A\x00\x14')),
        (lambda: display.draw_line(0x39C8, 0, 1, 2, 3),
         frame(0x03, b'\x39\xC8\x00\x00\x00\x01\x00\x02\x00\x03')),
        (lambda: display.draw_rectangle(1, 0x063B, 4, 5, 6, 7),
         frame(0x05, b'\x01\x06\x3B\x00\x04\x00\x05\x00\x06\x00\x07')),
        (lambda: display.draw_bitmap(0, 0, 8, 0xFFFF, 0x0000, b'\xAA'),
         frame(0x08, b'\x00\x00\x00\x00\x00\x08\xFF\xFF\x00\x00\xAA')),
        (lambda: display.move_area(1, 2, 16, 0x0000, 0, 0, 10, 10),
         frame(0x09, b'\x82\x00\x10\x00\x00\x00\x00\x00\x00\x00\x0A\x00\x0A')),
        (lambda: display.set_backlight(0x1F), frame(0x30, b'\x1F')),
        (lambda: display.show_jpeg(3), frame(0x22, b'\x00\x03')),
        (lambda: display.show_icon(2, 17, 100, 200),
         frame(0x23, b'\x00\x64\x00\xC8\x82\x11')),
        (lambda: display.copy_cache1(0, 0, 10, 10, 20, 20),
         frame(0x26, b'\x00\x00\x00\x00\x00\x0A\x00\x0A\x00\x14\x00\x14')),
        (lambda: display.set_animation_mask(0x0003), frame(0x29, b'\x00\x03')),
        (lambda: display.update(), frame(0x3D)),
    ]
    for call, expected in checks:
        transport.clear()
        call()
        assert transport.last_frame() == expected, (
            call, transport.last_frame().hex(), expected.hex())
    print('drawing: %d opcodes match the instruction set' % len(checks))


def test_orientation_uses_magic_prefix():
    display, transport = panel()
    display.set_orientation(1)
    assert transport.last_frame() == frame(0x34, b'\x5A\xA5\x01')
    print('orientation: 0x34 carries the 5A A5 guard')


def test_animation_mode_bits():
    display, transport = panel()
    display.configure_animation(1, True, 2, 166, 173, 60, 80, 10)
    #  mode = enabled<<7 | restart<<6 | id
    assert transport.last_frame() == frame(
        0x28, b'\x00\x3C\x00\x50\xC1\x02\xA6\xAD\x0A')
    display.configure_animation(0, False, 2, 1, 2, 0, 0, 5,
                                restart_from_first=False)
    assert transport.last_frame() == frame(
        0x28, b'\x00\x00\x00\x00\x00\x02\x01\x02\x05')
    print('animation: 0x28 mode bits verified')


def test_qr_and_ean13():
    display, transport = panel()
    display.draw_qr(8, 16, 3, b'hi')
    assert transport.last_frame() == frame(0x21, b'\x00\x08\x00\x10\x03hi')
    display.draw_ean13(10, 20, '123456789012')
    assert transport.last_frame() == frame(
        0x2A, b'\x00\x0A\x00\x14\x01\x02\x03\x04\x05\x06\x07\x08\x09\x00\x01\x02')
    for bad in ('12345678901', '12345678901x'):
        try:
            display.draw_ean13(0, 0, bad)
        except ValueError:
            pass
        else:
            raise AssertionError('expected ValueError for %r' % bad)
    try:
        display.draw_ean13(1, 0, '123456789012')     # odd x
    except ValueError:
        pass
    else:
        raise AssertionError('odd EAN-13 coordinates must be rejected')
    print('qr/ean13: payloads and validation verified')


# ----------------------------------------------------------------------
# Text and numbers
# ----------------------------------------------------------------------

def test_draw_text_mode_bits_and_payload():
    display, transport = panel()
    display.draw_text(False, True, display.FONT_8X16, 0xFFFF, 0x0000, 10, 20, 'OK')
    #  mode = width_adjust<<7 | show_background<<6 | font
    assert transport.last_frame() == frame(
        0x11, b'\x41\xFF\xFF\x00\x00\x00\x0A\x00\x14OK')
    transport.clear()
    display.draw_text(True, False, display.FONT_6X12, 0x0001, 0x0002, 0, 0, 'x')
    assert transport.last_frame() == frame(
        0x11, b'\x80\x00\x01\x00\x02\x00\x00\x00\x00x')
    print('text: 0x11 mode bits and payload verified')


def test_draw_text_folds_and_clips():
    display, transport = panel()
    display.draw_text(False, True, display.FONT_8X16, 0xFFFF, 0, 0, 0,
                      'Dusuk isi 45°C')
    payload = transport.payloads(0x11)[0][1]
    assert payload.endswith(b'Dusuk isi 45?C'), payload
    # Clipped to the frame: nothing is sent when y is off-panel.
    transport.clear()
    display.draw_text(False, True, display.FONT_8X16, 0xFFFF, 0, 0,
                      display.HEIGHT, 'offscreen')
    assert transport.frames == [], 'text below the frame must not be sent'
    # Width clip: a long string is truncated to the visible columns.
    transport.clear()
    display.draw_text(False, True, display.FONT_32X64, 0xFFFF, 0, 0, 0, 'X' * 40)
    payload = transport.payloads(0x11)[0][1]
    assert len(payload) - 9 == display.WIDTH // 32, len(payload)
    print('text: folding and clipping verified')


def test_draw_number_encoding():
    display, transport = panel()
    display.draw_number(1234, font=display.FONT_8X16, color=0xFFFF,
                        integer_digits=4, fractional_digits=0, x=1, y=2)
    assert transport.last_frame() == frame(
        0x14, b'\x01\xFF\xFF\x00\x00\x04\x00\x00\x01\x00\x02'
              b'\x00\x00\x04\xD2')
    transport.clear()
    display.draw_number(-2, signed=True, byte_length=2, integer_digits=1)
    payload = transport.payloads(0x14)[0][1]
    assert payload[0] == 0x41, hex(payload[0])      # signed bit set, font 1
    assert payload[-2:] == b'\xFF\xFE', payload[-2:].hex()
    try:
        display.draw_number(40000, byte_length=1)
    except ValueError:
        pass
    else:
        raise AssertionError('overflowing numeric value must be rejected')
    print('number: 0x14 encoding and overflow check verified')


def test_compatibility_text_renderers():
    display, transport = panel()
    display.draw_integer_text(True, False, False, display.FONT_8X16,
                              0xFFFF, 0, 3, 0, 0, 42)
    payload = transport.payloads(0x11)[0][1]
    assert payload[9:] == b' 42', payload[9:]
    transport.clear()
    display.draw_signed_scaled_float_text(display.FONT_8X16, 0, 2, 2, 50, 10, 1234)
    payload = transport.payloads(0x11)[0][1]
    # Signed mode reserves a leading sign column, so width is 2 + '.' + 2 + 1.
    assert payload[9:] == b' 12.34', payload[9:]
    transport.clear()
    display.draw_signed_scaled_float_text(display.FONT_8X16, 0, 2, 2, 50, 10, -1234)
    payload = transport.payloads(0x11)[0][1]
    assert payload[9:] == b'-12.34', payload[9:]
    print('compat renderers: right-aligned fixed point verified')


# ----------------------------------------------------------------------
# Framing
# ----------------------------------------------------------------------

def test_fragmented_rx_is_reassembled():
    display, transport = panel()
    display.begin_liveness_probe(timeout=5)
    for byte in frame(0x00, b'OK'):
        transport.feed(bytes((byte,)))
        result = display.poll_liveness_probe()
    assert result is True, 'a byte-at-a-time response must still parse'
    print('framing: byte-fragmented response reassembled')


def test_payload_containing_tail_is_not_split():
    # A 0x32 memory reply whose data happens to contain the trailer bytes.
    data = TAIL
    payload = b'\xA5\x01\x00' + bytes((len(data),)) + data
    buffer = bytearray(frame(0x32, payload))
    command, parsed = take_response(buffer)
    assert command == 0x32 and parsed == payload, parsed.hex()
    assert not buffer, 'the whole frame should be consumed'
    print('framing: trailer inside a payload does not split the frame')


def test_noise_resynchronises():
    buffer = bytearray(b'\x00\xff\x12' + frame(0x00, b'OK'))
    command, payload = take_response(buffer)
    assert command == 0x00 and payload == b'OK'
    print('framing: leading noise discarded, frame recovered')


def test_oversized_payload_rejected():
    display, transport = panel()
    try:
        display._send(0x39, b'\x00' * 249)
    except ValueError:
        pass
    else:
        raise AssertionError('payload over 248 bytes must be rejected')
    display.aux_write(b'\x00' * 248)
    print('framing: 248-byte data field boundary enforced')


def test_aux_frames_chunking():
    chunks = list(aux_frames(b'\x01' * 500))
    assert len(chunks) == 3, len(chunks)
    for chunk in chunks:
        assert chunk.startswith(b'\xAA\x39') and chunk.endswith(TAIL)
        assert len(chunk) - 6 <= 248
    rebuilt = b''.join(chunk[2:-4] for chunk in chunks)
    assert rebuilt == b'\x01' * 500
    print('aux: 0x39 chunking preserves the payload')


# ----------------------------------------------------------------------
# Memory
# ----------------------------------------------------------------------

def test_blocking_flash_write_waits_for_ack():
    display, transport = panel()
    transport.reply(0x31, b'\xA5OK')
    display.write_flash(0x0100, b'ABCD')
    command, payload = transport.payloads(0x31)[0]
    assert payload == b'\xA5\x01\x00ABCD', payload.hex()
    print('flash: blocking write sends 0x31 and consumes the A5 OK ACK')


def test_blocking_flash_read_matches_request():
    display, transport = panel()
    transport.reply(0x32, b'\xA5\x01\x00\x04WXYZ')
    assert display.read_flash(0x0100, 4) == b'WXYZ'
    command, payload = transport.payloads(0x32)[0]
    assert payload == b'\xA5\x01\x00\x04', payload.hex()
    print('flash: blocking read matches address and length')


def test_flash_read_spans_multiple_chunks():
    """A read longer than MEMORY_READ_CHUNK must split and reassemble in order."""
    length = 0x3F * 2 + 5
    contents = bytes((index & 0xFF) for index in range(0x4000))

    def panel_memory(transport, command, payload):
        if command != 0x32:
            return
        address = (payload[1] << 8) | payload[2]
        size = payload[3]
        transport.reply(0x32, payload[:4] + contents[address:address + size])

    transport = LoopbackTransport(responder=panel_memory)
    display = T5UIC1Display(transport, handshake=False, wake_delay=0)
    transport.clear()

    assert display.read_flash(0x0200, length) == contents[0x0200:0x0200 + length]
    requests = transport.payloads(0x32)
    assert len(requests) == 3, len(requests)
    assert [payload[3] for _, payload in requests] == [0x3F, 0x3F, 5]
    print('flash: %d-byte read split into %d chunks and reassembled'
          % (length, len(requests)))


def test_early_reply_for_later_chunk_is_purged():
    """A reply parsed before its own request was sent must not confirm it.

    Both chunk replies are queued up front. The first is consumed normally;
    the second is parsed into the frame queue while the driver is still
    waiting on the first, and is then discarded when chunk two is actually
    sent -- a reply that predates its request proves nothing about it. The
    read therefore times out rather than accepting unverified data.
    """
    display, transport = panel()
    transport.reply(0x32, b'\xA5\x02\x00' + bytes((0x3F,)) + b'\x11' * 0x3F)
    transport.reply(0x32, b'\xA5\x02\x3F\x04' + b'\x22' * 4)
    try:
        display.read_flash(0x0200, 0x3F + 4, timeout=0.05)
    except T5UIC1TimeoutError:
        pass
    else:
        raise AssertionError('an early reply must not satisfy a later request')
    print('framing: reply predating its request is purged, not trusted')


def test_sram_write_chunks_without_ack():
    """SRAM writes are not acknowledged, so they must not wait for one."""
    display, transport = panel()
    display.write_sram(0x0000, b'\x5A' * (display.MEMORY_WRITE_CHUNK + 10))
    writes = transport.payloads(0x31)
    assert len(writes) == 2, len(writes)
    assert writes[0][1][:3] == b'\x5A\x00\x00'
    assert len(writes[0][1]) - 3 == display.MEMORY_WRITE_CHUNK
    assert writes[1][1][:3] == b'\x5A\x00\x80', writes[1][1][:3].hex()
    print('sram: write chunked at 128 bytes with no ACK wait')


def test_flash_read_rejects_mismatched_reply():
    display, transport = panel()
    transport.reply(0x32, b'\xA5\x02\x00\x04WXYZ')     # wrong address
    try:
        display.read_flash(0x0100, 4, timeout=0.05)
    except T5UIC1TimeoutError:
        pass
    else:
        raise AssertionError('a mismatched memory reply must not be accepted')
    print('flash: mismatched reply rejected rather than returned')


def test_memory_bounds_enforced():
    display, _ = panel()
    for call in (lambda: display.read_flash(0x3FFE, 2),
                 lambda: display.write_flash(0x3FFE, b'xx'),
                 lambda: display.read_sram(display.SRAM_SIZE, 1)):
        try:
            call()
        except ValueError:
            continue
        raise AssertionError('out-of-range memory access must be rejected')
    # 0x3FFE is the last usable Data Flash byte.
    assert display.FLASH_USABLE_SIZE == 0x3FFF
    print('flash: 0x0000..0x3FFE usable window enforced')


def test_nonblocking_flash_write_verifies_by_readback():
    display, transport = panel()
    promise = display.begin_flash_write_verified(0x0400, b'PETBZR02')
    assert display.flash_busy()

    display.poll_flash_tasks()                     # sends the 0x31 write
    command, payload = transport.payloads(0x31)[0]
    assert payload == b'\xA5\x04\x00PETBZR02', payload.hex()
    assert not promise.done(), 'must not settle before readback'

    transport.reply(0x31, b'\xA5OK')
    display.poll_flash_tasks()                     # consumes ACK, moves to read
    display.poll_flash_tasks()                     # sends the 0x32 read
    command, payload = transport.payloads(0x32)[0]
    assert payload == b'\xA5\x04\x00\x08', payload.hex()

    transport.reply(0x32, b'\xA5\x04\x00\x08PETBZR02')
    display.poll_flash_tasks()
    assert promise.done() and promise.result() == b'PETBZR02'
    assert not display.flash_busy()
    print('flash: nonblocking write confirmed by exact readback')


def test_nonblocking_flash_detects_corruption():
    display, transport = panel()
    promise = display.begin_flash_write_verified(0x0400, b'GOOD')
    display.poll_flash_tasks()                     # sends the 0x31 write
    transport.reply(0x31, b'\xA5OK')
    display.poll_flash_tasks()                     # ACK consumed, read stage

    # Answer every readback with the wrong bytes. The driver must retry the
    # *read* a bounded number of times and then fail, never reissue the write.
    # Retries carry a real 0.15s-per-attempt back-off, so let the clock run.
    answered = 0
    deadline = time.monotonic() + 10
    while not promise.done() and time.monotonic() < deadline:
        display.poll_flash_tasks()
        reads = transport.payloads(0x32)
        if len(reads) > answered:
            answered = len(reads)
            transport.reply(0x32, b'\xA5\x04\x00\x04BAD!')
        time.sleep(0.01)

    assert promise.done(), 'bounded retries must settle the promise'
    assert promise.exception() is not None, 'corrupt readback must fail the promise'
    writes = [op for op, _ in transport.payloads() if op == 0x31]
    assert len(writes) == 1, 'verification must never reissue the write'
    assert answered >= 2, 'expected bounded read retries, got %d' % answered
    print('flash: readback mismatch fails after %d read retries, '
          'no rewrite' % answered)


def test_flash_queue_depth_limited():
    display, _ = panel()
    promises = [display.begin_flash_write_verified(0x0100 + index * 16, b'x' * 4)
                for index in range(6)]
    failed = [p for p in promises if p.done() and p.exception() is not None]
    assert failed, 'queue overflow must settle a promise with an error'
    print('flash: request queue depth bounded')


# ----------------------------------------------------------------------
# Helpers
# ----------------------------------------------------------------------

def test_colour_and_baud_helpers():
    assert rgb565(255, 255, 255) == 0xFFFF
    assert rgb565(0, 0, 0) == 0x0000
    assert rgb565(255, 0, 0) == 0xF800
    assert rgb565(0, 255, 0) == 0x07E0
    assert rgb565(0, 0, 255) == 0x001F
    # Stock Ender 3 V2: CFG divisor 0x0044 = 68 at 115200.
    divisor, actual = cfg_baud_divisor(115200)
    assert divisor == 68 and round(actual) == 115200, (divisor, actual)
    # The auxiliary UART runs off a different clock.
    aux_divisor, aux_actual = aux_baud_divisor(115200)
    assert aux_divisor == 136 and round(aux_actual) == 115200, (aux_divisor, aux_actual)
    try:
        aux_baud_divisor(9600)
    except ValueError:
        pass
    else:
        raise AssertionError('aux baud below 15300 must be rejected')
    print('helpers: rgb565 and both baud divisors verified')


def test_closed_display_rejects_commands():
    display, _ = panel()
    display.close()
    for call in (lambda: display.clear(0), lambda: display.update()):
        try:
            call()
        except RuntimeError:
            continue
        raise AssertionError('a closed display must reject commands')
    print('lifecycle: closed display rejects commands')


if __name__ == '__main__':
    tests = sorted((name, function) for name, function in globals().items()
                   if name.startswith('test_'))
    for name, function in tests:
        function()
    print('\nALL %d DRIVER TESTS PASSED' % len(tests))
