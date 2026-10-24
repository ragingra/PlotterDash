"""
A fake serial port that behaves like a Roland DXY-1200 for development without
hardware. Enable with ``PLOTTERDASH_SIMULATE=1``; the port list then contains
``SIM`` and connecting to it uses this class instead of pyserial.

Every ``OE;`` is answered with ``0\\r`` after a short delay proportional to the
amount of HPGL received, so progress bars move at a believable pace. ``OA;``
reports the tracked position and ``OH;`` the hard-clip limits, like the real
plotter does.
"""
from __future__ import annotations

import threading
import time

SIM_PORT = "SIM"
SIM_LIMITS = (0, 0, 16700, 11800)


class FakeSerial:
    def __init__(self, port: str = SIM_PORT, baudrate: int = 9600, timeout: float | None = None, **_):
        self.port = port
        self.baudrate = baudrate
        self.timeout = timeout
        self.is_open = True
        self._rx = bytearray()
        self._cv = threading.Condition()
        self._pending_delay = 0.0
        self.sent: list[str] = []
        self.pen = 0
        self.position = (0, 0)
        self.pen_down = False
        self.relative = False
        self.error_code = 0

    # pyserial-compatible surface ------------------------------------------
    def write(self, data: bytes) -> int:
        text = data.decode("utf-8", errors="replace")
        self.sent.append(text)
        for cmd in text.split(";"):
            cmd = cmd.strip()
            if not cmd:
                continue
            self._execute(cmd)
        return len(data)

    def read_until(self, expected: bytes = b";", size: int | None = None) -> bytes:
        deadline = None if self.timeout is None else time.monotonic() + self.timeout
        with self._cv:
            while not self._rx:
                remaining = None if deadline is None else deadline - time.monotonic()
                if remaining is not None and remaining <= 0:
                    return b""
                self._cv.wait(timeout=remaining)
            n = len(self._rx) if size is None else min(size, len(self._rx))
            out = bytes(self._rx[:n])
            del self._rx[:n]
            return out

    @property
    def in_waiting(self) -> int:
        return len(self._rx)

    def reset_input_buffer(self) -> None:
        with self._cv:
            self._rx.clear()

    def close(self) -> None:
        self.is_open = False
        with self._cv:
            self._cv.notify_all()

    # minimal HPGL interpretation -------------------------------------------
    def _reply(self, text: str) -> None:
        with self._cv:
            self._rx.extend(f"{text}\r".encode())
            self._cv.notify_all()

    def _execute(self, cmd: str) -> None:
        verb = cmd[:2].upper()
        args = cmd[2:]
        if verb == "OE":
            delay = self._pending_delay
            self._pending_delay = 0.0
            if delay > 0:
                time.sleep(min(delay, 2.0))
            self._reply(str(self.error_code))
            self.error_code = 0
        elif verb == "OA":
            self._reply(f"{self.position[0]},{self.position[1]},{int(self.pen_down)}")
        elif verb == "OH":
            self._reply(",".join(str(v) for v in SIM_LIMITS))
        elif verb == "SP":
            try:
                self.pen = int(args) if args else 0
            except ValueError:
                self.error_code = 3
            if self.pen < 0 or self.pen > 8:
                self.error_code = 3
            self._pending_delay += 0.3
        elif verb in ("PD", "PU"):
            self.pen_down = verb == "PD"
            if args:
                nums = args.split(",")
                self._pending_delay += 0.004 * len(nums)
                try:
                    values = [int(n) for n in nums]
                    if len(values) % 2:
                        raise ValueError
                    for x, y in zip(values[0::2], values[1::2]):
                        if self.relative:
                            self.position = (self.position[0] + x, self.position[1] + y)
                        else:
                            self.position = (x, y)
                except ValueError:
                    self.error_code = 2
            else:
                self._pending_delay += 0.05
        elif verb == "PA":
            self.relative = False
            self._pending_delay += 0.02
        elif verb == "PR":
            self.relative = True
            self._pending_delay += 0.02
        elif verb == "IN":
            self.relative = False
            self.pen_down = False
            self._pending_delay += 0.02
        elif verb in ("DF", "VS", "PS"):
            self._pending_delay += 0.02
        else:
            self.error_code = 1


def is_simulated_port(port: str | None) -> bool:
    return (port or "").upper() == SIM_PORT
