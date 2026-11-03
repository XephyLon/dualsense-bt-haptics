"""Paced delivery of haptic blocks to a hidraw node using a monotonic timerfd."""
from __future__ import annotations

import os
import time
from collections.abc import Callable
from dataclasses import dataclass

from .protocol import BUFFER_LENGTH, FRAME_BYTES, FRAME_SAMPLES, RATE, build_haptics_report

PERIOD_NS = round(1e9 * FRAME_SAMPLES / RATE)       # 10 666 667 ns per 64-byte block
SETTLE_REPORTS = 4                                  # silent reports sent at the end


@dataclass
class Stats:
    sent: int = 0
    late_ticks: int = 0
    write_errors: int = 0
    max_write_us: float = 0.0
    max_gap_us: float = 0.0
    seconds: float = 0.0

    def summary(self) -> str:
        return (f"{self.sent} reports in {self.seconds:.2f} s, late ticks {self.late_ticks}, "
                f"write errors {self.write_errors}, max write {self.max_write_us:.0f} us, "
                f"max gap {self.max_gap_us / 1000:.2f} ms")


class HapticStreamer:
    """Sends report 0x32 once per block period. `source` yields 64-byte blocks."""

    def __init__(self, path: str, double: bool = False, fd: int | None = None,
                 lead: int = 0, buffer_len: int = BUFFER_LENGTH):
        self.path = path
        self.double = double
        self.lead = lead                  # silent reports sent back-to-back before the first tick
        self.buffer_len = buffer_len      # controller-side buffer length byte
        self.fd = fd if fd is not None else os.open(path, os.O_WRONLY | os.O_CLOEXEC)
        self.seq = 0
        self.counter = 0
        self.stats = Stats()

    def send_frames(self, frames: bytes) -> None:
        report = build_haptics_report(frames, self.seq, self.counter, buffer_len=self.buffer_len)
        self.seq = (self.seq + 1) & 0x0F
        self.counter = (self.counter + len(frames) // FRAME_BYTES) & 0xFF
        t0 = time.perf_counter()
        try:
            os.write(self.fd, report)
        except OSError:
            self.stats.write_errors += 1
            raise
        self.stats.sent += 1
        self.stats.max_write_us = max(self.stats.max_write_us, (time.perf_counter() - t0) * 1e6)

    def run(self, source: Callable[[], bytes | None]) -> Stats:
        per_report = 2 if self.double else 1
        tfd = os.timerfd_create(time.CLOCK_MONOTONIC, flags=os.TFD_CLOEXEC)
        start = time.monotonic()
        last_tick = None
        try:
            # Priming: push `lead` silent reports at once so the controller's buffer runs
            # fuller than real time. Costs lead x block period of latency, buys that much
            # tolerance against Bluetooth stalls.
            silence = bytes(FRAME_BYTES * per_report)
            for _ in range(self.lead):
                self.send_frames(silence)
            os.timerfd_settime_ns(tfd, initial=1, interval=PERIOD_NS * per_report)
            while True:
                expirations = int.from_bytes(os.read(tfd, 8), "little")
                now = time.perf_counter_ns()
                if last_tick is not None:
                    self.stats.max_gap_us = max(self.stats.max_gap_us, (now - last_tick) / 1e3)
                last_tick = now
                if expirations > 1:
                    self.stats.late_ticks += expirations - 1
                chunks = []
                for _ in range(per_report):
                    block = source()
                    if block is None:
                        break
                    chunks.append(block)
                if not chunks:
                    break
                self.send_frames(b"".join(chunks))
            for _ in range(SETTLE_REPORTS):
                os.read(tfd, 8)
                self.send_frames(silence)
        finally:
            self.stats.seconds = time.monotonic() - start
            os.close(tfd)
        return self.stats
