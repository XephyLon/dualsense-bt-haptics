import io
import sys

import numpy as np
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
    assert len(blocks) == 94                  # 3000 frames / 32 = 93.75 → 94 blocks
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
    # 8-bit WAV is unsigned, centred on 128; 0.25 full scale → 160 → ≈ +32 after centring
    wavfile.write(path, 3000, np.full(96, 160, np.uint8))
    pcm = np.frombuffer(b"".join(drain(sources.wav(str(path)))), np.int8)
    assert np.array_equal(pcm[0::2], pcm[1::2])
    assert 28 <= pcm[0] <= 34


def test_stdin_source_reads_blocks_and_pads(monkeypatch):
    monkeypatch.setattr(sys, "stdin", io.TextIOWrapper(io.BytesIO(bytes(range(64)) + b"\x05\x06")))
    src = sources.stdin_s8()
    assert src() == bytes(range(64))
    assert src() == b"\x05\x06" + bytes(62)
    assert src() is None
