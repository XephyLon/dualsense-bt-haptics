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
