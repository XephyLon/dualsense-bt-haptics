# DualSense BT Haptics Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Stream haptic audio to a Bluetooth-connected DualSense / DualSense Edge on Linux: a Python CLI for tests and diagnostics, and a native PipeWire sink so any app can drive the haptics.

**Architecture:** Both tools build HID output report `0x32` (142 bytes: header, int8 stereo 3 kHz PCM, CRC-32 seeded with 0xA2) and write it to the controller's `/dev/hidrawN` once per 10.667 ms. The Python package `ds5bt` holds the protocol builder, block sources and a timerfd-paced sender. The C program `ds5-haptics-sink` is a `pw_stream` sink node (F32, 2 ch, 48 kHz, 512-frame quantum) that low-passes, decimates 16:1, converts to int8 and writes one report per process callback, with auto-reconnect.

**Tech Stack:** Python 3.14 (`os.timerfd_*`), numpy, scipy, pytest; C17 with libpipewire-0.3 and zlib; systemd user unit.

Spec: `docs/superpowers/specs/2026-11-03-ds5-bt-haptics-design.md`

---

## File structure

```
pyproject.toml                 package metadata, console script, pytest config
.gitignore
ds5bt/__init__.py
ds5bt/__main__.py              `python3 -m ds5bt` → cli.main()
ds5bt/protocol.py              constants, CRC, report builders, hidraw discovery (pure)
ds5bt/sources.py               block generators: tone, sweep, wav, stdin
ds5bt/stream.py                HapticStreamer: timerfd pacing, stats
ds5bt/cli.py                   argparse front end
tests/test_protocol.py
tests/test_sources.py
tests/test_stream.py
sink/ds5_haptics_sink.c        PipeWire sink
sink/Makefile
systemd/ds5-haptics-sink.service
README.md
```

Repo note: the project lives on a fuseblk (NTFS) mount; `git config core.fileMode false` is already set. Do not create symlinks. Run pytest as `python3 -m pytest` from the repo root so the `ds5bt` package is importable without installing.

---

### Task 1: Scaffolding

**Files:**
- Create: `pyproject.toml`, `.gitignore`, `ds5bt/__init__.py`, `ds5bt/__main__.py`

- [ ] **Step 1: Create files**

`pyproject.toml`:
```toml
[build-system]
requires = ["setuptools>=68"]
build-backend = "setuptools.build_meta"

[project]
name = "ds5bt"
version = "0.1.0"
description = "DualSense haptics over Bluetooth: protocol library and test tool"
requires-python = ">=3.13"
dependencies = ["numpy", "scipy"]

[project.scripts]
ds5bt = "ds5bt.cli:main"

[tool.setuptools]
packages = ["ds5bt"]

[tool.pytest.ini_options]
testpaths = ["tests"]
```

`.gitignore`:
```
__pycache__/
*.pyc
.pytest_cache/
sink/ds5-haptics-sink
*.egg-info/
build/
dist/
```

`ds5bt/__init__.py`:
```python
"""DualSense haptics over Bluetooth."""
```

`ds5bt/__main__.py`:
```python
from .cli import main

raise SystemExit(main())
```

- [ ] **Step 2: Commit**

```bash
git add pyproject.toml .gitignore ds5bt/__init__.py ds5bt/__main__.py
git commit -m "chore: scaffold ds5bt package"
```

---

### Task 2: Protocol — CRC and report 0x32

**Files:**
- Create: `ds5bt/protocol.py`
- Test: `tests/test_protocol.py`

- [ ] **Step 1: Write the failing tests**

`tests/test_protocol.py`:
```python
import zlib

import pytest

from ds5bt import protocol as p


def test_crc_is_zlib_crc32_over_seed_plus_data():
    data = bytes(range(60))
    assert p.crc32_sony(data) == zlib.crc32(b"\xA2" + data) & 0xFFFFFFFF


def test_crc_seed_state_matches_known_value():
    # zlib.crc32(b"\xA2") == 0xEADA2D49, the constant other implementations hard-code
    assert p.crc32_sony(b"") == 0xEADA2D49


def test_haptics_report_single_frame_layout():
    pcm = bytes(range(64))
    rep = p.build_haptics_report(pcm, seq=3, counter=7)
    assert len(rep) == 142
    assert rep[0] == 0x32
    assert rep[1] == 0x30            # seq in high nibble
    assert rep[2] == 0x91            # sub-packet 0x11, sized
    assert rep[3] == 4
    assert rep[4] == 0x66            # mic off
    assert rep[5] == 48 and rep[6] == 48
    assert rep[7] == 7               # counter
    assert rep[8] == 0x92            # sub-packet 0x12, single frame
    assert rep[9] == 64
    assert rep[10:74] == pcm
    assert rep[74:138] == bytes(64)
    assert int.from_bytes(rep[138:142], "little") == p.crc32_sony(rep[:138])


def test_haptics_report_double_frame_layout():
    pcm = bytes(128)
    rep = p.build_haptics_report(pcm, seq=0, counter=0)
    assert rep[8] == 0xD2
    assert rep[10:138] == pcm


def test_haptics_report_mic_flag():
    rep = p.build_haptics_report(bytes(64), seq=0, counter=0, mic=True)
    assert rep[4] == 0x67


def test_haptics_report_wraps_seq_and_counter():
    rep = p.build_haptics_report(bytes(64), seq=16 + 5, counter=256 + 9)
    assert rep[1] == 0x50
    assert rep[7] == 9


def test_haptics_report_rejects_bad_length():
    with pytest.raises(ValueError):
        p.build_haptics_report(bytes(63), seq=0, counter=0)
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python3 -m pytest tests/test_protocol.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'ds5bt.protocol'`

- [ ] **Step 3: Write the implementation**

`ds5bt/protocol.py`:
```python
"""DualSense Bluetooth HID output reports for haptic audio.

Report 0x32 carries raw signed 8-bit stereo PCM at 3000 Hz to the voice-coil
actuators. The byte layout follows the public reverse-engineering work of
SAxense (egormanga, https://apps.sdore.me/SAxense) and DS5Dongle (awalol);
this file is an independent implementation of that documented layout.
"""
from __future__ import annotations

import glob
import os
import zlib

RATE = 3000                  # haptic sample rate, Hz
FRAME_SAMPLES = 32           # stereo frames per 64-byte block
FRAME_BYTES = FRAME_SAMPLES * 2
BLOCK_SECONDS = FRAME_SAMPLES / RATE   # 10.667 ms

REPORT_HAPTICS = 0x32
REPORT_HAPTICS_SIZE = 142
REPORT_STATE = 0x31
REPORT_STATE_SIZE = 78
CRC_SEED_OUTPUT = 0xA2       # Bluetooth HID "DATA | OUTPUT" header byte
BUFFER_LENGTH = 48           # controller-side audio buffer, samples

VENDOR_SONY = "054C"
PRODUCTS = ("0CE6", "0DF2")  # DualSense, DualSense Edge
SYSFS_HID = "/sys/bus/hid/devices"
BUS_BLUETOOTH = "0005"


def crc32_sony(data: bytes, seed: int = CRC_SEED_OUTPUT) -> int:
    """CRC-32 the controller expects: zlib CRC over the seed byte then the data."""
    return zlib.crc32(bytes([seed]) + data) & 0xFFFFFFFF


def _finish(report: bytearray) -> bytes:
    report[-4:] = crc32_sony(bytes(report[:-4])).to_bytes(4, "little")
    return bytes(report)


def build_haptics_report(frames: bytes, seq: int, counter: int, mic: bool = False) -> bytes:
    """Build report 0x32 holding one (64 B) or two (128 B) int8 stereo blocks."""
    if len(frames) not in (FRAME_BYTES, 2 * FRAME_BYTES):
        raise ValueError(f"frames must be {FRAME_BYTES} or {2 * FRAME_BYTES} bytes, got {len(frames)}")
    double = len(frames) == 2 * FRAME_BYTES
    r = bytearray(REPORT_HAPTICS_SIZE)
    r[0] = REPORT_HAPTICS
    r[1] = (seq & 0x0F) << 4
    r[2] = 0x11 | 0x80                       # sub-packet 0x11: audio control, sized
    r[3] = 4                                 # number of set bits in r[4]
    r[4] = 0x66 | (0x01 if mic else 0x00)    # bit5 buffer-length valid, bit6 mandatory, bit0 mic
    r[5] = BUFFER_LENGTH
    r[6] = BUFFER_LENGTH
    r[7] = counter & 0xFF
    r[8] = 0x12 | (0x40 if double else 0x00) | 0x80   # sub-packet 0x12: haptic PCM
    r[9] = FRAME_BYTES
    r[10:10 + len(frames)] = frames
    return _finish(r)


def build_state_report(seq: int, *, haptic_lpf: bool | None = None,
                       rumble_power_reduction: int | None = None,
                       trigger_power_reduction: int | None = None) -> bytes:
    """Build report 0x31 touching only the haptic low-pass filter and/or motor power level.

    SetStateData starts at byte 3. flags1 (byte 4): bit5 = AllowHapticLowPassFilter,
    bit6 = AllowMotorPowerLevel. Byte 39 = MotorPowerLevel (rumble<<4 | trigger, x12.5 %).
    Byte 42 bit0 = HapticLowPassFilter.
    """
    r = bytearray(REPORT_STATE_SIZE)
    r[0] = REPORT_STATE
    r[1] = (seq & 0x0F) << 4
    r[2] = 0x10
    s = 3
    if haptic_lpf is not None:
        r[s + 1] |= 0x20
        r[s + 39] = 0x01 if haptic_lpf else 0x00
    if rumble_power_reduction is not None or trigger_power_reduction is not None:
        rp = rumble_power_reduction or 0
        tp = trigger_power_reduction or 0
        if not (0 <= rp <= 7 and 0 <= tp <= 7):
            raise ValueError("power reduction must be 0..7")
        r[s + 1] |= 0x40
        r[s + 36] = (rp << 4) | tp
    return _finish(r)


def find_hidraw(sysfs: str = SYSFS_HID) -> list[str]:
    """Return /dev/hidrawN paths of DualSense controllers connected over Bluetooth."""
    paths: list[str] = []
    for pid in PRODUCTS:
        pattern = os.path.join(sysfs, f"{BUS_BLUETOOTH}:{VENDOR_SONY}:{pid}.*", "hidraw", "hidraw*")
        for node in sorted(glob.glob(pattern)):
            paths.append("/dev/" + os.path.basename(node))
    return paths
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python3 -m pytest tests/test_protocol.py -v`
Expected: 7 passed

- [ ] **Step 5: Commit**

```bash
git add ds5bt/protocol.py tests/test_protocol.py
git commit -m "feat(protocol): report 0x32 builder and Sony CRC-32"
```

---

### Task 3: Protocol — report 0x31 knobs and hidraw discovery

**Files:**
- Modify: `tests/test_protocol.py` (append)
- `ds5bt/protocol.py` already contains the implementation from Task 2

- [ ] **Step 1: Append the tests**

Append to `tests/test_protocol.py`:
```python
def test_state_report_lpf_on_sets_only_flag_and_byte():
    rep = p.build_state_report(seq=1, haptic_lpf=True)
    assert len(rep) == 78
    assert rep[0] == 0x31 and rep[1] == 0x10 and rep[2] == 0x10
    assert rep[4] == 0x20          # flags1: AllowHapticLowPassFilter
    assert rep[42] == 0x01         # HapticLowPassFilter on
    touched = {i for i, b in enumerate(rep[:74]) if b}
    assert touched == {0, 1, 2, 4, 42}
    assert int.from_bytes(rep[74:78], "little") == p.crc32_sony(rep[:74])


def test_state_report_lpf_off():
    rep = p.build_state_report(seq=0, haptic_lpf=False)
    assert rep[4] == 0x20 and rep[42] == 0x00


def test_state_report_power_reduction():
    rep = p.build_state_report(seq=0, rumble_power_reduction=3, trigger_power_reduction=1)
    assert rep[4] == 0x40
    assert rep[39] == 0x31


def test_state_report_power_reduction_range():
    with pytest.raises(ValueError):
        p.build_state_report(seq=0, rumble_power_reduction=8)


def test_find_hidraw_bluetooth_only(tmp_path):
    def mk(hid_id, hidraw):
        d = tmp_path / hid_id / "hidraw" / hidraw
        d.mkdir(parents=True)
    mk("0005:054C:0DF2.001C", "hidraw12")     # Edge over BT
    mk("0005:054C:0CE6.0003", "hidraw4")      # DualSense over BT
    mk("0003:054C:0CE6.0002", "hidraw3")      # DualSense over USB: ignored
    mk("0005:046D:B034.0001", "hidraw9")      # other BT device: ignored
    assert p.find_hidraw(str(tmp_path)) == ["/dev/hidraw4", "/dev/hidraw12"]


def test_find_hidraw_empty(tmp_path):
    assert p.find_hidraw(str(tmp_path)) == []
```

- [ ] **Step 2: Run tests**

Run: `python3 -m pytest tests/test_protocol.py -v`
Expected: 13 passed. If `test_state_report_lpf_on_sets_only_flag_and_byte` fails on the `touched` set, the offsets in `build_state_report` are wrong; fix the implementation, not the test.

- [ ] **Step 3: Commit**

```bash
git add tests/test_protocol.py ds5bt/protocol.py
git commit -m "feat(protocol): 0x31 LPF/power knobs and Bluetooth hidraw discovery"
```

---

### Task 4: Block sources

**Files:**
- Create: `ds5bt/sources.py`
- Test: `tests/test_sources.py`

- [ ] **Step 1: Write the failing tests**

`tests/test_sources.py`:
```python
import io
import sys

import numpy as np
import pytest
from scipy.io import wavfile

from ds5bt import sources
from ds5bt.protocol import FRAME_BYTES, RATE


def drain(src):
    out = []
    while (b := src()) is not None:
        out.append(b)
    return out


def test_tone_block_count_and_size():
    blocks = drain(sources.tone(100.0, seconds=1.0))
    assert len(blocks) == RATE // 32          # 3000 frames / 32 = 93.75 → 94 blocks
    assert all(len(b) == FRAME_BYTES for b in blocks)


def test_tone_left_only_leaves_right_silent():
    pcm = np.frombuffer(b"".join(drain(sources.tone(100.0, 0.5, channel="left"))), np.int8)
    assert np.abs(pcm[0::2]).max() > 100
    assert not pcm[1::2].any()


def test_tone_right_only():
    pcm = np.frombuffer(b"".join(drain(sources.tone(100.0, 0.5, channel="right"))), np.int8)
    assert not pcm[0::2].any()
    assert np.abs(pcm[1::2]).max() > 100


def test_gain_scales_and_clips():
    quiet = np.frombuffer(b"".join(drain(sources.tone(50.0, 0.2, gain=0.5))), np.int8)
    loud = np.frombuffer(b"".join(drain(sources.tone(50.0, 0.2, gain=4.0))), np.int8)
    assert 60 <= np.abs(quiet).max() <= 64
    assert np.abs(loud).max() == 127 or loud.min() == -128


def test_sweep_runs_and_pads_to_blocks():
    blocks = drain(sources.sweep(40.0, 1000.0, seconds=0.1))
    assert len(blocks) == 10                  # 300 frames → ceil(300/32) = 10
    assert all(len(b) == FRAME_BYTES for b in blocks)


def test_sweep_same_frequency_is_a_tone():
    blocks = drain(sources.sweep(100.0, 100.0, seconds=0.1))
    assert len(blocks) == 10


def test_wav_resamples_and_maps_channels(tmp_path):
    rate = 48000
    t = np.arange(rate) / rate
    left = (0.5 * np.sin(2 * np.pi * 60 * t) * 32767).astype(np.int16)
    right = np.zeros(rate, np.int16)
    path = tmp_path / "t.wav"
    wavfile.write(path, rate, np.stack([left, right], axis=1))
    pcm = np.frombuffer(b"".join(drain(sources.wav(str(path)))), np.int8)
    assert len(pcm) // 2 in range(RATE, RATE + 32)       # 1 s at 3 kHz, padded to a block
    assert 55 <= np.abs(pcm[0::2]).max() <= 70            # 0.5 * 127 ≈ 63
    assert np.abs(pcm[1::2]).max() <= 1


def test_wav_mono_is_duplicated(tmp_path):
    path = tmp_path / "m.wav"
    wavfile.write(path, 3000, (np.ones(96) * 0.25 * 127).astype(np.int8).view(np.uint8) + 128)
    # 8-bit WAV is unsigned; 0.25 full scale ≈ +32 after centring
    pcm = np.frombuffer(b"".join(drain(sources.wav(str(path)))), np.int8)
    assert np.array_equal(pcm[0::2], pcm[1::2])
    assert 28 <= pcm[0] <= 34


def test_stdin_source_reads_blocks_and_pads(monkeypatch):
    monkeypatch.setattr(sys, "stdin", io.TextIOWrapper(io.BytesIO(bytes(range(64)) + b"\x05\x06")))
    src = sources.stdin_s8()
    assert src() == bytes(range(64))
    assert src() == b"\x05\x06" + bytes(62)
    assert src() is None
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python3 -m pytest tests/test_sources.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'ds5bt.sources'`

- [ ] **Step 3: Write the implementation**

`ds5bt/sources.py`:
```python
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
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python3 -m pytest tests/test_sources.py -v`
Expected: 10 passed

- [ ] **Step 5: Commit**

```bash
git add ds5bt/sources.py tests/test_sources.py
git commit -m "feat(sources): tone, sweep, wav and stdin block sources"
```

---

### Task 5: Paced streamer

**Files:**
- Create: `ds5bt/stream.py`
- Test: `tests/test_stream.py`

- [ ] **Step 1: Write the failing tests**

`tests/test_stream.py`:
```python
import os
import time

import pytest

from ds5bt import protocol as p
from ds5bt.stream import PERIOD_NS, HapticStreamer


def blocks_source(blocks):
    it = iter(blocks)
    return lambda: next(it, None)


def read_all(fd):
    out = b""
    while True:
        try:
            chunk = os.read(fd, 65536)
        except BlockingIOError:
            break
        if not chunk:
            break
        out += chunk
    return out


def test_period_is_one_block():
    assert PERIOD_NS == 10_666_667


def test_streams_blocks_then_four_silent_reports_at_pace():
    r, w = os.pipe()
    os.set_blocking(r, False)
    st = HapticStreamer("fake", fd=w)
    blocks = [bytes([i]) * 64 for i in (1, 2, 3)]
    t0 = time.monotonic()
    stats = st.run(blocks_source(blocks))
    elapsed = time.monotonic() - t0
    data = read_all(r)
    os.close(r)
    reports = [data[i:i + 142] for i in range(0, len(data), 142)]
    assert len(reports) == 7                     # 3 data + 4 silence
    assert [rep[0] for rep in reports] == [0x32] * 7
    assert [rep[1] >> 4 for rep in reports] == [0, 1, 2, 3, 4, 5, 6]
    assert [rep[7] for rep in reports] == [0, 1, 2, 3, 4, 5, 6]
    assert reports[0][10:74] == bytes([1]) * 64
    assert reports[3][10:74] == bytes(64)
    assert all(int.from_bytes(rep[138:], "little") == p.crc32_sony(rep[:138]) for rep in reports)
    assert stats.sent == 7
    assert 0.06 <= elapsed <= 0.5                # 7 ticks × 10.67 ms ≈ 75 ms


def test_double_mode_packs_two_blocks_and_odd_tail():
    r, w = os.pipe()
    os.set_blocking(r, False)
    st = HapticStreamer("fake", fd=w, double=True)
    st.run(blocks_source([bytes([1]) * 64, bytes([2]) * 64, bytes([3]) * 64]))
    data = read_all(r)
    os.close(r)
    reports = [data[i:i + 142] for i in range(0, len(data), 142)]
    assert len(reports) == 2 + 4
    assert reports[0][8] == 0xD2 and reports[0][10:138] == bytes([1]) * 64 + bytes([2]) * 64
    assert reports[1][8] == 0x92 and reports[1][10:74] == bytes([3]) * 64
    assert [rep[7] for rep in reports[:2]] == [0, 2]


def test_write_error_is_counted_and_raised():
    r, w = os.pipe()
    os.close(r)                                   # writes now fail with EPIPE
    st = HapticStreamer("fake", fd=w)
    with pytest.raises(OSError):
        st.run(blocks_source([bytes(64)]))
    assert st.stats.write_errors == 1


def test_summary_mentions_counts():
    st = HapticStreamer("fake", fd=os.open(os.devnull, os.O_WRONLY))
    st.stats.sent = 10
    assert "10" in st.stats.summary()
```

Note: `SIGPIPE` is ignored by default in Python, so a closed pipe raises `BrokenPipeError`.

- [ ] **Step 2: Run tests to verify they fail**

Run: `python3 -m pytest tests/test_stream.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'ds5bt.stream'`

- [ ] **Step 3: Write the implementation**

`ds5bt/stream.py`:
```python
"""Paced delivery of haptic blocks to a hidraw node using a monotonic timerfd."""
from __future__ import annotations

import os
import time
from collections.abc import Callable
from dataclasses import dataclass

from .protocol import FRAME_BYTES, FRAME_SAMPLES, RATE, build_haptics_report

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

    def __init__(self, path: str, double: bool = False, fd: int | None = None):
        self.path = path
        self.double = double
        self.fd = fd if fd is not None else os.open(path, os.O_WRONLY | os.O_CLOEXEC)
        self.seq = 0
        self.counter = 0
        self.stats = Stats()

    def send_frames(self, frames: bytes) -> None:
        report = build_haptics_report(frames, self.seq, self.counter)
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
            silence = bytes(FRAME_BYTES * per_report)
            for _ in range(SETTLE_REPORTS):
                os.read(tfd, 8)
                self.send_frames(silence)
        finally:
            self.stats.seconds = time.monotonic() - start
            os.close(tfd)
        return self.stats
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python3 -m pytest tests/test_stream.py -v`
Expected: 5 passed

- [ ] **Step 5: Commit**

```bash
git add ds5bt/stream.py tests/test_stream.py
git commit -m "feat(stream): timerfd-paced report sender with stats"
```

---

### Task 6: CLI

**Files:**
- Create: `ds5bt/cli.py`

- [ ] **Step 1: Write the implementation**

`ds5bt/cli.py`:
```python
"""ds5bt — DualSense haptics over Bluetooth test tool."""
from __future__ import annotations

import argparse
import os
import sys

from . import protocol, sources
from .stream import HapticStreamer


def _try_realtime() -> bool:
    try:
        os.sched_setscheduler(0, os.SCHED_FIFO, os.sched_param(20))
        return True
    except (PermissionError, OSError):
        return False


def _device(args) -> str:
    if args.device:
        return args.device
    devs = protocol.find_hidraw()
    if not devs:
        sys.exit("ds5bt: no DualSense over Bluetooth. Looked for "
                 "/sys/bus/hid/devices/0005:054C:{0CE6,0DF2}.*/hidraw. "
                 "USB-connected controllers are ignored on purpose.")
    return devs[0]


def cmd_info(args) -> int:
    devs = protocol.find_hidraw()
    if not devs:
        print("no DualSense connected over Bluetooth")
        return 1
    for dev in devs:
        name = "?"
        try:
            with open(f"/sys/class/hidraw/{os.path.basename(dev)}/device/uevent") as f:
                for line in f:
                    if line.startswith("HID_NAME="):
                        name = line.strip().split("=", 1)[1]
        except OSError:
            pass
        access = "rw" if os.access(dev, os.R_OK | os.W_OK) else "NO ACCESS"
        print(f"{dev}\t{name}\t{access}")
    return 0


def _run(args, source) -> int:
    dev = _device(args)
    try:
        st = HapticStreamer(dev, double=args.double)
    except PermissionError:
        sys.exit(f"ds5bt: {dev}: permission denied. Needs the udev uaccess rule for "
                 "054C:0DF2 / 054C:0CE6 hidraw nodes, or rw access to the device.")
    if args.lpf is not None or args.power_reduction is not None:
        rep = protocol.build_state_report(st.seq, haptic_lpf=None if args.lpf is None else args.lpf == "on",
                                          rumble_power_reduction=args.power_reduction)
        os.write(st.fd, rep)
        st.seq = (st.seq + 1) & 0x0F
    rt = _try_realtime()
    print(f"ds5bt: {dev}, {'double' if args.double else 'single'} frame reports, "
          f"realtime {'on' if rt else 'off'}", file=sys.stderr)
    try:
        st.run(source)
    except OSError as e:
        print(f"ds5bt: write failed: {e} (controller disconnected?)", file=sys.stderr)
    except KeyboardInterrupt:
        pass
    print(st.stats.summary(), file=sys.stderr)
    return 0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="ds5bt", description="DualSense haptics over Bluetooth")
    ap.add_argument("--device", help="hidraw path (default: first Bluetooth DualSense)")
    ap.add_argument("--double", action="store_true", help="two blocks per report (21.3 ms cadence)")
    ap.add_argument("--gain", type=float, default=1.0)
    ap.add_argument("--lpf", choices=["on", "off"], help="set the controller's haptic low-pass filter first")
    ap.add_argument("--power-reduction", type=int, choices=range(8), metavar="0-7",
                    help="rumble motor power reduction, x12.5%% steps")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("info", help="list Bluetooth DualSense hidraw nodes")
    t = sub.add_parser("tone", help="sine tone")
    t.add_argument("hz", type=float)
    t.add_argument("--sec", type=float, default=2.0)
    t.add_argument("--ch", choices=["left", "right", "both"], default="both")
    s = sub.add_parser("sweep", help="exponential sweep on both actuators")
    s.add_argument("f0", type=float)
    s.add_argument("f1", type=float)
    s.add_argument("--sec", type=float, default=6.0)
    w = sub.add_parser("wav", help="play a WAV file (L→left, R→right)")
    w.add_argument("path")
    sub.add_parser("stdin", help="raw s8 stereo 3 kHz from stdin")
    args = ap.parse_args(argv)

    if args.cmd == "info":
        return cmd_info(args)
    if args.cmd == "tone":
        src = sources.tone(args.hz, args.sec, args.ch, args.gain)
    elif args.cmd == "sweep":
        src = sources.sweep(args.f0, args.f1, args.sec, args.gain)
    elif args.cmd == "wav":
        src = sources.wav(args.path, args.gain)
    else:
        src = sources.stdin_s8()
    return _run(args, src)
```

- [ ] **Step 2: Smoke test without hardware**

Run: `python3 -m ds5bt --help && python3 -m ds5bt info`
Expected: help text; then one line per connected controller, e.g. `/dev/hidraw12  DualSense Edge Wireless Controller  rw`, or "no DualSense connected over Bluetooth" with exit code 1.

Run: `python3 -m ds5bt --device /dev/null tone 100 --sec 0.1`
Expected: stderr `ds5bt: /dev/null, single frame reports, realtime off` then a stats line with `14 reports` (10 blocks + 4 silence) and `late ticks 0`.

- [ ] **Step 3: Commit**

```bash
git add ds5bt/cli.py
git commit -m "feat(cli): ds5bt info/tone/sweep/wav/stdin"
```

---

### Task 7: Hardware validation on the DualSense Edge

**Files:**
- Modify: `README.md` (create; results section)

Prerequisite: controller connected over Bluetooth (`python3 -m ds5bt info` shows it).

- [ ] **Step 1: Left / right / both tones**

Run, feeling the controller between each:
```bash
python3 -m ds5bt tone 80 --sec 2 --ch left
python3 -m ds5bt tone 80 --sec 2 --ch right
python3 -m ds5bt tone 80 --sec 2 --ch both
```
Expected: vibration in the left grip only, then right only, then both. Each prints a stats line with `late ticks 0` and no write errors. If nothing is felt, try `--gain 2`, then `--double`; if still nothing, capture one run with `sudo btmon -w /tmp/ds5.btsnoop` and check the 0x32 frames reach the controller.

- [ ] **Step 2: Sweep and WAV**

```bash
python3 -m ds5bt sweep 40 1000 --sec 6
ffmpeg -loglevel error -i /usr/share/sounds/freedesktop/stereo/complete.oga -ac 2 /tmp/claude-1000/-mnt-games2-AI-dualsensee/d94d13b8-2985-4a3a-b751-c623f3320409/scratchpad/complete.wav 2>/dev/null || true
python3 -m ds5bt wav /tmp/claude-1000/-mnt-games2-AI-dualsensee/d94d13b8-2985-4a3a-b751-c623f3320409/scratchpad/complete.wav
```
Expected: rising pitch with no stutter; the WAV is recognisable.

- [ ] **Step 3: Record results in README**

Create `README.md` with a short usage section and a "Tested" section recording controller model, firmware/kernel, Bluetooth adapter, which commands worked and the stats line. Commit:

```bash
git add README.md
git commit -m "docs: README with hardware test results"
```

---

### Task 8: PipeWire sink (C)

**Files:**
- Create: `sink/ds5_haptics_sink.c`, `sink/Makefile`

- [ ] **Step 1: Write the Makefile**

`sink/Makefile`:
```make
CC      ?= gcc
CFLAGS  ?= -O2 -Wall -Wextra -std=gnu17
PKGS     = libpipewire-0.3 zlib
CFLAGS  += $(shell pkg-config --cflags $(PKGS))
LDLIBS   = $(shell pkg-config --libs $(PKGS)) -lm

TARGET   = ds5-haptics-sink
PREFIX  ?= $(HOME)/.local
UNIT_DIR = $(HOME)/.config/systemd/user

$(TARGET): ds5_haptics_sink.c
	$(CC) $(CFLAGS) -o $@ $< $(LDLIBS)

install: $(TARGET)
	install -Dm755 $(TARGET) $(PREFIX)/bin/$(TARGET)

service: install
	install -Dm644 ../systemd/ds5-haptics-sink.service $(UNIT_DIR)/ds5-haptics-sink.service
	systemctl --user daemon-reload
	systemctl --user enable --now ds5-haptics-sink.service

service-uninstall:
	-systemctl --user disable --now ds5-haptics-sink.service
	rm -f $(UNIT_DIR)/ds5-haptics-sink.service
	systemctl --user daemon-reload

clean:
	rm -f $(TARGET)

.PHONY: install service service-uninstall clean
```

- [ ] **Step 2: Write the sink**

`sink/ds5_haptics_sink.c`:
```c
/*
 * ds5-haptics-sink: PipeWire sink that streams audio to a DualSense /
 * DualSense Edge's voice-coil haptics over Bluetooth.
 *
 * Protocol: HID output report 0x32 carrying int8 stereo PCM at 3 kHz, CRC-32
 * seeded with the Bluetooth HID header byte 0xA2. Byte layout per the public
 * reverse-engineering work of SAxense (egormanga, https://apps.sdore.me/SAxense)
 * and DS5Dongle (awalol); this is an independent implementation.
 *
 * Audio path: F32 2ch 48 kHz from PipeWire -> 4th-order Butterworth low-pass
 * at 1.2 kHz -> decimate 16:1 -> gain/clip -> int8 -> one report per 32 frames.
 */
#define _GNU_SOURCE
#include <errno.h>
#include <fcntl.h>
#include <getopt.h>
#include <glob.h>
#include <math.h>
#include <signal.h>
#include <stdatomic.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <time.h>
#include <unistd.h>

#include <pipewire/pipewire.h>
#include <spa/param/audio/format-utils.h>
#include <zlib.h>

#define IN_RATE        48000
#define HAP_RATE       3000
#define DECIM          (IN_RATE / HAP_RATE)   /* 16 */
#define FRAME_SAMPLES  32                     /* stereo frames per block */
#define FRAME_BYTES    (FRAME_SAMPLES * 2)
#define REPORT_SIZE    142
#define STATE_SIZE     78
#define RING_FRAMES    4096
#define IDLE_BLOCKS    47                     /* ~500 ms of silence -> stop sending */
#define LPF_HZ         1200.0f

struct biquad { float b0, b1, b2, a1, a2, z1, z2; };

static void biquad_lowpass(struct biquad *q, float fs, float fc, float Q)
{
	float w0 = 2.0f * (float)M_PI * fc / fs;
	float cw = cosf(w0), sw = sinf(w0), alpha = sw / (2.0f * Q);
	float a0 = 1.0f + alpha;
	q->b0 = ((1.0f - cw) / 2.0f) / a0;
	q->b1 = (1.0f - cw) / a0;
	q->b2 = q->b0;
	q->a1 = (-2.0f * cw) / a0;
	q->a2 = (1.0f - alpha) / a0;
	q->z1 = q->z2 = 0.0f;
}

static inline float biquad_run(struct biquad *q, float x)
{
	float y = q->b0 * x + q->z1;
	q->z1 = q->b1 * x - q->a1 * y + q->z2;
	q->z2 = q->b2 * x - q->a2 * y;
	return y;
}

struct data {
	struct pw_main_loop *loop;
	struct pw_stream *stream;
	struct spa_source *timer;

	atomic_int fd;              /* hidraw fd, -1 when disconnected */
	atomic_int write_failed;    /* set by RT thread, handled by main loop */
	char dev_override[256];
	float gain;
	int lpf;                    /* -1 leave alone, 0 off, 1 on */
	int power_reduction;        /* -1 leave alone, 0..7 */
	int verbose;

	struct biquad lp[2][2];     /* [channel][stage] */
	int decim_phase;
	int8_t ring[RING_FRAMES * 2];
	int ring_len;               /* frames */
	int silent_blocks;
	uint8_t seq, counter;

	long sent, idle_skips, errors, process_calls, dropped;
	long max_write_us;
	time_t last_stats;
};

static uint32_t sony_crc(const uint8_t *buf, size_t n)
{
	static const uint8_t seed = 0xA2;
	uint32_t c = crc32(0L, Z_NULL, 0);
	c = crc32(c, &seed, 1);
	return crc32(c, buf, n);
}

static void put_crc(uint8_t *rep, size_t size)
{
	uint32_t c = sony_crc(rep, size - 4);
	rep[size - 4] = c & 0xff;
	rep[size - 3] = (c >> 8) & 0xff;
	rep[size - 2] = (c >> 16) & 0xff;
	rep[size - 1] = (c >> 24) & 0xff;
}

static void build_haptics_report(struct data *d, uint8_t out[REPORT_SIZE], const int8_t *pcm, int frames)
{
	int dbl = frames == 2 * FRAME_SAMPLES;
	memset(out, 0, REPORT_SIZE);
	out[0] = 0x32;
	out[1] = (uint8_t)(d->seq << 4);
	out[2] = 0x91;            /* sub-packet 0x11, sized */
	out[3] = 4;               /* popcount of out[4] */
	out[4] = 0x66;            /* buffer-length valid, mandatory bit, mic off */
	out[5] = 48;
	out[6] = 48;
	out[7] = d->counter;
	out[8] = dbl ? 0xD2 : 0x92;   /* sub-packet 0x12, bit6 = two frames */
	out[9] = FRAME_BYTES;
	memcpy(out + 10, pcm, (size_t)frames * 2);
	put_crc(out, REPORT_SIZE);
	d->seq = (d->seq + 1) & 0x0F;
	d->counter = (uint8_t)(d->counter + (dbl ? 2 : 1));
}

static void send_state_report(struct data *d, int fd)
{
	uint8_t rep[STATE_SIZE] = {0};
	rep[0] = 0x31;
	rep[1] = (uint8_t)(d->seq << 4);
	rep[2] = 0x10;
	if (d->lpf >= 0) {
		rep[4] |= 0x20;                     /* AllowHapticLowPassFilter */
		rep[42] = d->lpf ? 0x01 : 0x00;
	}
	if (d->power_reduction >= 0) {
		rep[4] |= 0x40;                     /* AllowMotorPowerLevel */
		rep[39] = (uint8_t)(d->power_reduction << 4);
	}
	put_crc(rep, STATE_SIZE);
	d->seq = (d->seq + 1) & 0x0F;
	if (write(fd, rep, STATE_SIZE) != STATE_SIZE)
		fprintf(stderr, "ds5-haptics-sink: state report failed: %s\n", strerror(errno));
}

static void send_haptics(struct data *d, const int8_t *pcm, int frames)
{
	uint8_t rep[REPORT_SIZE];
	int fd = atomic_load(&d->fd);
	if (fd < 0)
		return;
	build_haptics_report(d, rep, pcm, frames);
	struct timespec a, b;
	clock_gettime(CLOCK_MONOTONIC, &a);
	ssize_t n = write(fd, rep, REPORT_SIZE);
	clock_gettime(CLOCK_MONOTONIC, &b);
	long us = (b.tv_sec - a.tv_sec) * 1000000L + (b.tv_nsec - a.tv_nsec) / 1000;
	if (us > d->max_write_us)
		d->max_write_us = us;
	if (n == REPORT_SIZE) {
		d->sent++;
	} else if (n < 0 && errno == EAGAIN) {
		d->dropped++;
	} else {
		d->errors++;
		atomic_store(&d->write_failed, 1);
	}
}

/* Drain whole blocks from the ring: two per report when available, else one. */
static void emit_ready_blocks(struct data *d)
{
	while (d->ring_len >= FRAME_SAMPLES) {
		int frames = d->ring_len >= 2 * FRAME_SAMPLES ? 2 * FRAME_SAMPLES : FRAME_SAMPLES;
		int silent = 1;
		for (int i = 0; i < frames * 2 && silent; i++)
			if (d->ring[i] != 0)
				silent = 0;
		d->silent_blocks = silent ? d->silent_blocks + frames / FRAME_SAMPLES : 0;
		if (d->silent_blocks > IDLE_BLOCKS)
			d->idle_skips++;
		else
			send_haptics(d, d->ring, frames);
		d->ring_len -= frames;
		memmove(d->ring, d->ring + frames * 2, (size_t)d->ring_len * 2);
	}
}

static inline int8_t to_int8(float x)
{
	if (x > 127.0f) x = 127.0f;
	if (x < -127.0f) x = -127.0f;
	return (int8_t)lrintf(x);
}

static void on_process(void *userdata)
{
	struct data *d = userdata;
	struct pw_buffer *b = pw_stream_dequeue_buffer(d->stream);
	if (!b)
		return;
	struct spa_buffer *buf = b->buffer;
	const float *in = buf->datas[0].data;
	if (in) {
		uint32_t n = buf->datas[0].chunk->size / (sizeof(float) * 2);
		float g = d->gain * 127.0f;
		d->process_calls++;
		for (uint32_t i = 0; i < n; i++) {
			float l = biquad_run(&d->lp[0][1], biquad_run(&d->lp[0][0], in[2 * i]));
			float r = biquad_run(&d->lp[1][1], biquad_run(&d->lp[1][0], in[2 * i + 1]));
			if (++d->decim_phase == DECIM) {
				d->decim_phase = 0;
				if (d->ring_len < RING_FRAMES) {
					d->ring[2 * d->ring_len] = to_int8(l * g);
					d->ring[2 * d->ring_len + 1] = to_int8(r * g);
					d->ring_len++;
				}
			}
		}
		emit_ready_blocks(d);
	}
	pw_stream_queue_buffer(d->stream, b);
}

static void on_state_changed(void *userdata, enum pw_stream_state old, enum pw_stream_state state, const char *error)
{
	struct data *d = userdata;
	(void)old;
	if (d->verbose || error)
		fprintf(stderr, "ds5-haptics-sink: stream %s%s%s\n", pw_stream_state_as_string(state),
			error ? ": " : "", error ? error : "");
	if (state == PW_STREAM_STATE_ERROR)
		pw_main_loop_quit(d->loop);
}

static const struct pw_stream_events stream_events = {
	PW_VERSION_STREAM_EVENTS,
	.process = on_process,
	.state_changed = on_state_changed,
};

static int find_hidraw(char *out, size_t n)
{
	static const char *patterns[] = {
		"/sys/bus/hid/devices/0005:054C:0CE6.*/hidraw/hidraw*",
		"/sys/bus/hid/devices/0005:054C:0DF2.*/hidraw/hidraw*",
	};
	for (size_t p = 0; p < sizeof(patterns) / sizeof(patterns[0]); p++) {
		glob_t g;
		int rc = glob(patterns[p], 0, NULL, &g);
		if (rc == 0 && g.gl_pathc > 0) {
			const char *base = strrchr(g.gl_pathv[0], '/');
			snprintf(out, n, "/dev%s", base);
			globfree(&g);
			return 0;
		}
		if (rc == 0)
			globfree(&g);
	}
	return -1;
}

static void print_stats(struct data *d)
{
	fprintf(stderr, "ds5-haptics-sink: sent %ld, idle-skipped %ld, dropped %ld, errors %ld, "
		"process calls %ld, max write %ld us\n",
		d->sent, d->idle_skips, d->dropped, d->errors, d->process_calls, d->max_write_us);
}

/* Runs on the main loop once per second: handles disconnects and reconnects. */
static void on_timer(void *userdata, uint64_t expirations)
{
	struct data *d = userdata;
	(void)expirations;

	if (atomic_load(&d->write_failed)) {
		int fd = atomic_exchange(&d->fd, -1);
		if (fd >= 0)
			close(fd);
		atomic_store(&d->write_failed, 0);
		fprintf(stderr, "ds5-haptics-sink: controller write failed, waiting for it to come back\n");
	}
	if (atomic_load(&d->fd) < 0) {
		char path[256];
		if (d->dev_override[0])
			snprintf(path, sizeof(path), "%s", d->dev_override);
		else if (find_hidraw(path, sizeof(path)) < 0)
			goto stats;
		int fd = open(path, O_WRONLY | O_NONBLOCK | O_CLOEXEC);
		if (fd < 0) {
			if (errno != ENOENT)
				fprintf(stderr, "ds5-haptics-sink: open %s: %s\n", path, strerror(errno));
			goto stats;
		}
		if (d->lpf >= 0 || d->power_reduction >= 0)
			send_state_report(d, fd);
		atomic_store(&d->fd, fd);
		fprintf(stderr, "ds5-haptics-sink: connected to %s\n", path);
	}
stats:
	if (d->verbose) {
		time_t now = time(NULL);
		if (now - d->last_stats >= 60) {
			d->last_stats = now;
			print_stats(d);
		}
	}
}

static void do_quit(void *userdata, int signal_number)
{
	struct data *d = userdata;
	(void)signal_number;
	pw_main_loop_quit(d->loop);
}

static void usage(void)
{
	fprintf(stderr,
		"usage: ds5-haptics-sink [--gain F] [--lpf on|off] [--power-reduction 0-7]\n"
		"                        [--device /dev/hidrawN] [--verbose]\n"
		"PipeWire sink \"DualSense Haptics (BT)\": audio routed to it plays on the\n"
		"controller's haptic actuators over Bluetooth. DS5_HAPTICS_GAIN env sets gain.\n");
}

int main(int argc, char **argv)
{
	struct data d = { .fd = -1, .gain = 1.0f, .lpf = -1, .power_reduction = -1 };
	const char *env_gain = getenv("DS5_HAPTICS_GAIN");
	if (env_gain)
		d.gain = strtof(env_gain, NULL);

	static const struct option opts[] = {
		{ "gain", required_argument, 0, 'g' },
		{ "lpf", required_argument, 0, 'l' },
		{ "power-reduction", required_argument, 0, 'p' },
		{ "device", required_argument, 0, 'd' },
		{ "verbose", no_argument, 0, 'v' },
		{ "help", no_argument, 0, 'h' },
		{ 0, 0, 0, 0 },
	};
	int c;
	while ((c = getopt_long(argc, argv, "g:l:p:d:vh", opts, NULL)) != -1) {
		switch (c) {
		case 'g': d.gain = strtof(optarg, NULL); break;
		case 'l': d.lpf = strcmp(optarg, "on") == 0; break;
		case 'p': d.power_reduction = atoi(optarg) & 7; break;
		case 'd': snprintf(d.dev_override, sizeof(d.dev_override), "%s", optarg); break;
		case 'v': d.verbose = 1; break;
		default: usage(); return c == 'h' ? 0 : 2;
		}
	}

	/* 4th-order Butterworth = two 2nd-order sections with Q = 0.5412 and 1.3066 */
	for (int ch = 0; ch < 2; ch++) {
		biquad_lowpass(&d.lp[ch][0], IN_RATE, LPF_HZ, 0.5412f);
		biquad_lowpass(&d.lp[ch][1], IN_RATE, LPF_HZ, 1.3066f);
	}

	pw_init(&argc, &argv);
	d.loop = pw_main_loop_new(NULL);
	struct pw_loop *loop = pw_main_loop_get_loop(d.loop);
	pw_loop_add_signal(loop, SIGINT, do_quit, &d);
	pw_loop_add_signal(loop, SIGTERM, do_quit, &d);

	d.stream = pw_stream_new_simple(loop, "DualSense Haptics (BT)",
		pw_properties_new(
			PW_KEY_MEDIA_TYPE, "Audio",
			PW_KEY_MEDIA_CATEGORY, "Playback",
			PW_KEY_MEDIA_CLASS, "Audio/Sink",
			PW_KEY_NODE_NAME, "ds5_haptics",
			PW_KEY_NODE_DESCRIPTION, "DualSense Haptics (BT)",
			PW_KEY_NODE_LATENCY, "512/48000",        /* one 10.667 ms block per cycle */
			PW_KEY_DEVICE_ICON_NAME, "input-gaming",
			PW_KEY_AUDIO_CHANNELS, "2",
			"audio.position", "FL,FR",
			NULL),
		&stream_events, &d);

	uint8_t buffer[1024];
	struct spa_pod_builder b = SPA_POD_BUILDER_INIT(buffer, sizeof(buffer));
	struct spa_audio_info_raw info = SPA_AUDIO_INFO_RAW_INIT(
		.format = SPA_AUDIO_FORMAT_F32,
		.rate = IN_RATE,
		.channels = 2,
		.position = { SPA_AUDIO_CHANNEL_FL, SPA_AUDIO_CHANNEL_FR });
	const struct spa_pod *params[1];
	params[0] = spa_format_audio_raw_build(&b, SPA_PARAM_EnumFormat, &info);

	if (pw_stream_connect(d.stream, PW_DIRECTION_INPUT, PW_ID_ANY,
			PW_STREAM_FLAG_AUTOCONNECT | PW_STREAM_FLAG_MAP_BUFFERS | PW_STREAM_FLAG_RT_PROCESS,
			params, 1) < 0) {
		fprintf(stderr, "ds5-haptics-sink: pw_stream_connect failed\n");
		return 1;
	}

	d.timer = pw_loop_add_timer(loop, on_timer, &d);
	struct timespec first = { 0, 1000000 }, every = { 1, 0 };
	pw_loop_update_timer(loop, d.timer, &first, &every, false);

	fprintf(stderr, "ds5-haptics-sink: sink \"DualSense Haptics (BT)\" up, gain %.2f\n", d.gain);
	pw_main_loop_run(d.loop);

	print_stats(&d);
	int fd = atomic_exchange(&d.fd, -1);
	if (fd >= 0) {
		/* a few silent blocks so the coils settle */
		int8_t zero[FRAME_BYTES] = {0};
		for (int i = 0; i < 4; i++) {
			uint8_t rep[REPORT_SIZE];
			build_haptics_report(&d, rep, zero, FRAME_SAMPLES);
			if (write(fd, rep, REPORT_SIZE) < 0) break;
			usleep(10667);
		}
		close(fd);
	}
	pw_stream_destroy(d.stream);
	pw_main_loop_destroy(d.loop);
	pw_deinit();
	return 0;
}
```

- [ ] **Step 3: Build**

Run: `make -C sink`
Expected: compiles with no warnings. If `SPA_AUDIO_INFO_RAW_INIT` is unavailable, replace with a plain designated initialiser `{ .format = ..., .rate = ..., .channels = 2, .position = {...} }`.

- [ ] **Step 4: Commit**

```bash
git add sink/ds5_haptics_sink.c sink/Makefile
git commit -m "feat(sink): native PipeWire sink streaming haptics over Bluetooth"
```

---

### Task 9: Sink hardware validation

- [ ] **Step 1: Run the sink and confirm the node**

Run in the background: `./sink/ds5-haptics-sink --verbose &`
Then: `pactl list sinks short | grep ds5_haptics`
Expected: stderr shows `connected to /dev/hidraw12` and the sink is listed.

- [ ] **Step 2: Play through it**

```bash
pw-play --target ds5_haptics /tmp/claude-1000/-mnt-games2-AI-dualsensee/d94d13b8-2985-4a3a-b751-c623f3320409/scratchpad/complete.wav
```
Expected: the controller plays the sound as haptics. Also test routing a running app with `pactl move-sink-input <id> ds5_haptics`.

- [ ] **Step 3: Idle and reconnect**

Stop playback, wait 2 s; with `--verbose` the 60 s stats line shows idle-skipped growing while sent stays flat. Hold the PS button to power the controller off, wait 5 s, power it back on: log shows "write failed, waiting" then "connected to /dev/hidrawN". Play again: works without restarting the sink.

- [ ] **Step 4: Process cadence**

Compare `process calls` with wall time in the stats line: about 93.75 calls per second (512/48000). If calls come in at ~47/s with 1024-frame buffers, the double-frame path is being used; both are fine.

- [ ] **Step 5: Record results in README and commit**

```bash
git add README.md
git commit -m "docs: sink test results"
```

---

### Task 10: systemd unit, README, credits

**Files:**
- Create: `systemd/ds5-haptics-sink.service`
- Modify: `README.md`

- [ ] **Step 1: Unit file**

`systemd/ds5-haptics-sink.service`:
```ini
[Unit]
Description=DualSense Haptics (BT) PipeWire sink
After=pipewire.service wireplumber.service
Requires=pipewire.service

[Service]
ExecStart=%h/.local/bin/ds5-haptics-sink
Restart=on-failure
RestartSec=2
Nice=-10

[Install]
WantedBy=default.target
```

- [ ] **Step 2: README**

`README.md` must cover: what it does, requirements (Linux, hid-playstation, PipeWire, controller paired over Bluetooth, udev uaccess rule), `ds5bt` usage, building/installing the sink (`make -C sink`, `make -C sink service`), tuning (`--gain`, `--lpf`), known limitations (8-bit 3 kHz, clock drift, rumble from Steam overrides haptics, no game auto-routing yet), the hardware test results from Tasks 7 and 9, and a Credits section naming SAxense (with link https://apps.sdore.me/SAxense), DS5Dongle, chiaki-ng PR 858, ds5-bt-haptics, LinuxAudio4Dualsense5.

- [ ] **Step 3: Verify the service target works**

Run: `make -C sink service && systemctl --user status ds5-haptics-sink --no-pager | head -5`
Expected: active (running). Then `systemctl --user disable --now ds5-haptics-sink` if the user did not ask for it to stay enabled.

- [ ] **Step 4: Commit**

```bash
git add systemd/ds5-haptics-sink.service README.md
git commit -m "feat: systemd user unit and README"
```

---

## Self-review

- Spec coverage: protocol (Tasks 2–3), ds5bt sources/stream/cli (4–6), hardware tests (7, 9), C sink with LPF/decimate/idle/reconnect/0x31 knobs (8), systemd + docs + credits (10). Error handling: no controller / permission denied in `cli._device`/`_run`; disconnect in `stream.send_frames` + `sink on_timer`.
- Types: `build_haptics_report(frames, seq, counter, mic)` used identically in stream.py and tests; `Source = Callable[[], bytes | None]` consistent between sources.py and stream.run; `Stats.summary()` used by cli.
- No placeholders remain.
