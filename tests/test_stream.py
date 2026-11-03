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
