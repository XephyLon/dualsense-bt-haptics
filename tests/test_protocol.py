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


def test_haptics_report_buffer_len_byte_and_range():
    rep = p.build_haptics_report(bytes(64), seq=0, counter=0, buffer_len=127)
    assert rep[5] == 127 and rep[6] == 127
    with pytest.raises(ValueError):
        p.build_haptics_report(bytes(64), seq=0, counter=0, buffer_len=200)
