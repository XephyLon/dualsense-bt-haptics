#!/usr/bin/env python3
"""Generate the 3 s stereo test WAV used for hardware checks.

Layout: 0.2-0.7 s 60 Hz pulse LEFT, 1.0-1.5 s 60 Hz pulse RIGHT,
2.0-3.0 s tone rising 60 -> 200 Hz on BOTH. 48 kHz, 16-bit.

    python3 tools/make_test_wav.py /tmp/test.wav
    pw-play --target ds5_haptics /tmp/test.wav
"""
import sys

import numpy as np
from scipy.io import wavfile


def main(path: str) -> None:
    rate = 48000
    t = np.arange(3 * rate) / rate

    def env(start: float, end: float) -> np.ndarray:
        inside = (t >= start) & (t < end)
        return inside * np.sin(np.pi * np.clip((t - start) / (end - start), 0, 1))

    pulse = np.sin(2 * np.pi * 60 * t)
    rise = np.sin(2 * np.pi * (60 * (t - 2.0) + 70 * (t - 2.0) ** 2))  # 60 -> ~200 Hz over 1 s
    left = env(0.2, 0.7) * pulse + env(2.0, 3.0) * rise
    right = env(1.0, 1.5) * pulse + env(2.0, 3.0) * rise
    pcm = (np.stack([left, right], axis=1) * 0.9 * 32767).astype(np.int16)
    wavfile.write(path, rate, pcm)
    print(f"{path}: 0.2-0.7 s LEFT 60 Hz | 1.0-1.5 s RIGHT 60 Hz | 2.0-3.0 s BOTH rising")


if __name__ == "__main__":
    main(sys.argv[1] if len(sys.argv) > 1 else "test.wav")
