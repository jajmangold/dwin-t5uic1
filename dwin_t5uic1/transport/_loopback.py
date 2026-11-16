# SPDX-License-Identifier: GPL-3.0-or-later
"""Hardware-free transport double.

Records every frame the driver transmits and lets a test feed bytes back as
though the panel had answered, including in arbitrary fragments so the
length-aware RX parser is actually exercised.
"""


class LoopbackTransport:
    """In-memory transport for tests and for previewing a UI's packet stream."""

    def __init__(self, auto_handshake=False, responder=None):
        """``responder(transport, command, payload)`` runs on every frame sent.

        Use it to answer a request at the moment it is transmitted, the way
        real hardware does. Pre-queueing a reply before its request is sent
        does not work: the driver purges stale replies per opcode on each
        send, so an answer that arrived early is correctly discarded.
        """
        self.written = bytearray()
        self.frames = []
        self.closed = False
        self._inbound = bytearray()
        self.auto_handshake = auto_handshake
        self.responder = responder

    # -- transport surface -------------------------------------------

    @property
    def in_waiting(self):
        return len(self._inbound)

    def read(self, size):
        data = bytes(self._inbound[:size])
        self._inbound[:] = self._inbound[size:]   # MicroPython: no del on bytearray
        return data

    def write(self, data):
        if self.closed:
            raise OSError('transport is closed')
        frame = bytes(data)
        self.written.extend(data)
        self.frames.append(frame)
        if self.auto_handshake and frame == b'\xAA\x00\xCC\x33\xC3\x3C':
            self.reply(0x00, b'OK')
        if self.responder is not None and len(frame) >= 6:
            self.responder(self, frame[1], frame[2:-4])
        return len(data)

    def close(self):
        self.closed = True

    # -- test helpers ------------------------------------------------

    def reply(self, command, payload=b''):
        """Queue a well-formed inbound frame."""
        self._inbound.extend(b'\xAA' + bytes((command,)) + bytes(payload)
                             + b'\xCC\x33\xC3\x3C')

    def feed(self, data):
        """Queue arbitrary raw bytes, including noise or partial frames."""
        self._inbound.extend(data)

    def last_frame(self):
        return self.frames[-1] if self.frames else None

    def payloads(self, command=None):
        """Decode transmitted frames into ``(command, payload)`` pairs."""
        result = []
        for frame in self.frames:
            if len(frame) < 6 or frame[0] != 0xAA or frame[-4:] != b'\xCC\x33\xC3\x3C':
                raise AssertionError('malformed transmitted frame: %r' % frame)
            op, payload = frame[1], frame[2:-4]
            if command is None or op == command:
                result.append((op, payload))
        return result

    def clear(self):
        self.written[:] = b''
        del self.frames[:]
