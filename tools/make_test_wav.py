#!/usr/bin/env python3
"""Generate the 3 s stereo test WAV used for hardware checks.

Layout: 0.2-0.7 s 60 Hz pulse LEFT, 1.0-1.5 s 60 Hz pulse RIGHT,
2.0-3.0 s tone rising 60 -> 200 Hz on BOTH. 48 kHz, 16-bit.

    python3 tools/make_test_wav.py /tmp/test.wav
    pw-play --target ds5_haptics /tmp/test.wav

--4ch writes a 4-channel file for the wired/virtual DualSense audio device:
channels 1-2 (speaker) silent, channels 3-4 (haptic actuators) carry the pattern.
"""
import sys

import numpy as np
from scipy.io import wavfile


def main(path: str, four_channel: bool = False) -> None:
    rate = 48000
    t = np.arange(3 * rate) / rate

    def env(start: float, end: float) -> np.ndarray:
        inside = (t >= start) & (t < end)
        return inside * np.sin(np.pi * np.clip((t - start) / (end - start), 0, 1))

    pulse = np.sin(2 * np.pi * 60 * t)
    rise = np.sin(2 * np.pi * (60 * (t - 2.0) + 70 * (t - 2.0) ** 2))  # 60 -> ~200 Hz over 1 s
    left = env(0.2, 0.7) * pulse + env(2.0, 3.0) * rise
    right = env(1.0, 1.5) * pulse + env(2.0, 3.0) * rise
    chans = [left, right]
    if four_channel:
        silent = np.zeros_like(left)
        chans = [silent, silent, left, right]
    pcm = (np.stack(chans, axis=1) * 0.9 * 32767).astype(np.int16)
    wavfile.write(path, rate, pcm)
    layout = "4ch, haptics on channels 3-4" if four_channel else "stereo"
    print(f"{path} ({layout}): 0.2-0.7 s LEFT 60 Hz | 1.0-1.5 s RIGHT 60 Hz | 2.0-3.0 s BOTH rising")


if __name__ == "__main__":
    args = [a for a in sys.argv[1:] if a != "--4ch"]
    main(args[0] if args else "test.wav", four_channel="--4ch" in sys.argv)
