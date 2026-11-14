# SPDX-License-Identifier: GPL-3.0-or-later
"""Generic Linux input backend reading an evdev character device.

This is the backend for hosts with no usable GPIO header: an x86 box, a VM or
a container. Wire the panel's encoder to any USB device the kernel already
exposes as an input device (a USB volume knob, a rotary-encoder breakout
running QMK, a cheap presenter remote) and point this at its ``/dev/input``
node.

Unlike the GPIO backends this needs no root if the user is in the ``input``
group. ``libevdev``/``python-evdev`` must be installed.
"""

from evdev import InputDevice, ecodes, list_devices

# Relative axes a knob may report, and the key codes a press may arrive as.
_ROTARY_AXES = (ecodes.REL_DIAL, ecodes.REL_WHEEL, ecodes.REL_X)
_BUTTON_KEYS = (ecodes.KEY_ENTER, ecodes.KEY_KPENTER, ecodes.BTN_LEFT,
                ecodes.KEY_SPACE, ecodes.BTN_MIDDLE)


def find_devices():
    """List ``(path, name)`` for every readable input device."""
    found = []
    for path in list_devices():
        try:
            device = InputDevice(path)
        except OSError:
            continue
        found.append((path, device.name))
        device.close()
    return found


class EvdevEncoder:
    """Encoder plus button from a Linux input device.

    ``path`` is a node such as ``/dev/input/event5``, or a ``by-id`` symlink,
    which survives replugging. Pass ``axes``/``keys`` to override which event
    codes count as rotation and as a press.
    """

    def __init__(self, path, detent_counts=1, callback=None,
                 axes=_ROTARY_AXES, keys=_BUTTON_KEYS):
        if not isinstance(detent_counts, int) or detent_counts < 1:
            raise ValueError('detent_counts must be a positive integer')
        self._device = InputDevice(path)
        self._device.grab()
        self._detent_counts = detent_counts
        self._callback = callback
        self._axes = tuple(axes)
        self._keys = tuple(keys)
        self._counts = 0
        self._position = 0
        self._claimed = 0
        self._presses = 0

    @property
    def name(self):
        return self._device.name

    def poll(self):
        """Drain pending kernel events. Call this once per UI tick.

        Non-blocking: ``read()`` raises ``BlockingIOError`` when the queue is
        empty, which is the normal idle case rather than an error.
        """
        try:
            events = list(self._device.read())
        except BlockingIOError:
            return
        for event in events:
            if event.type == ecodes.EV_REL and event.code in self._axes:
                self._counts += event.value
                counts = self._counts
                position = (counts // self._detent_counts if counts >= 0
                            else -((-counts) // self._detent_counts))
                if position != self._position:
                    self._position = position
                    if self._callback is not None:
                        self._callback(position)
            elif event.type == ecodes.EV_KEY and event.code in self._keys:
                if event.value == 1:              # key down only
                    self._presses += 1

    def position(self):
        self.poll()
        return self._position

    def take_delta(self):
        self.poll()
        position = self._position
        delta = position - self._claimed
        self._claimed = position
        return delta

    def pressed(self):
        self.poll()
        if self._presses:
            self._presses -= 1
            return True
        return False

    def diagnostics(self):
        return {'counts': self._counts, 'position': self._position,
                'pending_presses': self._presses, 'device': self._device.name}

    def close(self):
        self._callback = None
        try:
            self._device.ungrab()
        except OSError:
            pass
        self._device.close()
