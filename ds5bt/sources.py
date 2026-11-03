"""Block sources. A source is a zero-argument callable returning the next
64-byte block of int8 interleaved stereo PCM at 3 kHz, or None when done."""
from __future__ import annotations

import math
import sys
from collections.abc import Callable, Iterator

import numpy as np

from .protocol import FRAME_BYTES, FRAME_SAMPLES, RATE

Source = Callable[[], bytes | None]


def to_int8_blocks(left: np.ndarray, right: np.ndarray, gain: float = 1.0) -> Iterator[bytes]:
    """Interleave two float arrays in [-1, 1], scale, clip to int8, pad to whole blocks."""
    n = max(len(left), len(right))
    total = n + (-n) % FRAME_SAMPLES
    inter = np.zeros(total * 2, np.float64)
    inter[0:2 * len(left):2] = left
    inter[1:2 * len(right):2] = right
    pcm = np.clip(np.rint(inter * (gain * 127.0)), -128, 127).astype(np.int8).tobytes()
    for i in range(0, len(pcm), FRAME_BYTES):
        yield pcm[i:i + FRAME_BYTES]


def from_iter(it: Iterator[bytes]) -> Source:
    return lambda: next(it, None)


def tone(hz: float, seconds: float, channel: str = "both", gain: float = 1.0) -> Source:
    n = int(round(seconds * RATE))
    t = np.arange(n) / RATE
    w = np.sin(2 * math.pi * hz * t)
    zero = np.zeros(n)
    left = w if channel in ("left", "both") else zero
    right = w if channel in ("right", "both") else zero
    return from_iter(to_int8_blocks(left, right, gain))


def sweep(f0: float, f1: float, seconds: float, gain: float = 1.0) -> Source:
    """Exponential chirp from f0 to f1 on both channels."""
    n = int(round(seconds * RATE))
    t = np.arange(n) / RATE
    if f0 == f1:
        phase = 2 * math.pi * f0 * t
    else:
        k = (f1 / f0) ** (1.0 / seconds)
        phase = 2 * math.pi * f0 * (k ** t - 1) / math.log(k)
    w = np.sin(phase)
    return from_iter(to_int8_blocks(w, w, gain))


def _to_float(data: np.ndarray) -> np.ndarray:
    if data.dtype.kind == "f":
        return data.astype(np.float64)
    info = np.iinfo(data.dtype)
    x = data.astype(np.float64)
    if data.dtype.kind == "u":                     # 8-bit WAV is unsigned, centred on 128
        half = (info.max + 1) / 2
        return (x - half) / half
    return x / (info.max + 1)


def wav(path: str, gain: float = 1.0) -> Source:
    """Any PCM/float WAV. Stereo L/R → left/right actuator; mono is duplicated."""
    from scipy.io import wavfile
    from scipy.signal import resample_poly

    rate, data = wavfile.read(path)
    x = _to_float(data)
    if x.ndim == 1:
        x = np.stack([x, x], axis=1)
    if rate != RATE:
        g = math.gcd(rate, RATE)
        x = resample_poly(x, RATE // g, rate // g, axis=0)
    return from_iter(to_int8_blocks(x[:, 0], x[:, 1], gain))


def stdin_s8() -> Source:
    """Raw signed 8-bit stereo 3 kHz from stdin (ffmpeg -f s8 -ar 3000 -ac 2 -)."""
    f = sys.stdin.buffer

    def src() -> bytes | None:
        b = f.read(FRAME_BYTES)
        if not b:
            return None
        return b.ljust(FRAME_BYTES, b"\x00")

    return src
