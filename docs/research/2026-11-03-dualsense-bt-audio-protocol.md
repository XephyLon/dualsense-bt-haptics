# DualSense Bluetooth audio / haptics protocol — research notes

Date: 2026-11-03. Compiled from public reverse-engineering projects (cloned and
read at the commits listed at the end) plus our own measurements on a DualSense
Edge (054C:0DF2) over a MediaTek MT7921/7922 Bluetooth 5.2 adapter, Arch Linux
kernel 7.2.11, BlueZ 5.87, PipeWire 1.6.9. Nothing here comes from Sony
documentation. Where projects disagree or guess, it is marked.

## 1. Why Sony's PC story says "wired only"

Over USB the controller exposes a USB Audio Class device with 4 output
channels (1–2 speaker/headphones, 3–4 left/right haptic actuator) plus a mic.
Games with native DualSense haptics write audio to that device. Over Bluetooth
no audio device appears on PC, so games have nothing to write to. The
controller **does** accept audio over Bluetooth; the PS5 uses it. The transport
is plain HID output reports on the interrupt channel, not A2DP, not a
proprietary BT profile. Everything below is reachable from an unprivileged
`write()` to `/dev/hidrawN` on Linux.

Windows status: DSX v3.2 Beta 01 (June 2026) added BT haptics via a virtual
wired device. `yyusvf/ds5-bt-haptics` does the same with usbip-win2.
`awalol/DS5Dongle` is a Pico 2 W hardware dongle doing it in firmware.
Linux: `egormanga/SAxense` (first public POC, Jan 2026), chiaki-ng PR 858,
`sendement/dualsense-haptics`, `ebraminio/LinuxAudio4Dualsense5` (speaker).

## 2. Bluetooth output report inventory

From the BT HID report descriptor (nondebug/dualsense), vendor usage page
0xFF00, all 8-bit fields; sizes include the report-ID byte:

| ID | bytes | known use |
|---|---|---|
| 0x31 | 78 | SetStateData (LEDs, triggers, rumble emulation, audio routing/volume). Kernel and Steam use this. |
| 0x32 | 142 | audio control + haptic PCM, haptics only (1 or 2 blocks) |
| 0x33 | 206 | unknown, same family |
| 0x34 | 270 | unknown |
| 0x35 | 334 | unknown |
| 0x36 | 398 | audio control + 1 haptic block + 1 Opus speaker frame |
| 0x37 | 462 | unknown |
| 0x38 | 526 | unknown |
| 0x39 | 547 | audio control + 2 haptic blocks + 2 Opus speaker frames |

Every BT output report ends with a 4-byte little-endian CRC-32:
`zlib.crc32(b"\xA2" + report[:-4])`. 0xA2 is the Bluetooth HID header byte
(DATA | OUTPUT) that precedes the report on the wire. Other seeds seen:
0xA3 for GET_REPORT feature replies (dualsense-haptics `FEATURE_CRC_SEED`),
0x53 for SET_REPORT feature writes (DS5Dongle `FillFeatureCrc`). Equivalent
constant: `zlib.crc32(b"\xA2") == 0xEADA2D49`, which SAxense hard-codes as the
inverted initial state.

Byte 1 of every BT output report: `seq << 4`, a 4-bit sequence counter in the
high nibble, low nibble 0. DS5Dongle shares one counter across all reports.
SAxense leaves it at 0 permanently and works, so the firmware does not appear
to enforce it. Byte 2 of report 0x31 is the "tag" 0x10 (kernel: "exact meaning
unclear"). For 0x32/0x36/0x39 byte 2 is instead the first sub-packet header.

## 3. Sub-packet container (reports 0x32, 0x36, 0x39)

After the ID and sequence byte, the payload is a sequence of sub-packets:

```
header byte:  bit7 = "sized" (always set in practice)
              bit6 = "double": the data block holds two frames
              bits0-5 = packet id
length byte:  size of ONE frame of data
data:         length bytes, or 2 x length when bit6 is set
```

Known packet ids:

| id | header (single / double) | content |
|---|---|---|
| 0x10 | 0x90 | SetStateData block (63 B), only in DS5Dongle v0.7.2's old layout; now sent as a separate 0x31 |
| 0x11 | 0x91 | audio control |
| 0x12 | 0x92 / 0xD2 | haptic PCM, 64 B per frame |
| 0x13 | 0x93 / 0xD3 | speaker Opus frame, 200 B per frame |
| 0x16 | 0x96 / 0xD6 | same Opus frame but routed to the headphone jack (DS5Dongle "lock headphone") |

### 3.1 Audio control packet 0x11

```
[0] 0x91
[1] N   = popcount of the mask byte
[2] mask
[3..] one byte per set mask bit, EXCEPT bit6, in MSB-first order
```

Observed instances (all work on real hardware):

| mask | N | bytes that follow | source |
|---|---|---|---|
| 0x02 | 1 | (none; counter implied 0) | DS5Dongle `update_mic_status` |
| 0x66 | 4 | buf, buf, counter | DS5Dongle 0x32 haptics-only |
| 0x7E | 6 | buf, buf, buf, buf, counter | DS5Dongle 0x39 |
| 0xFE | 7 | buf, buf, buf, buf, buf, counter | DS5Dongle 0x36; SAxense 0x32 (with zeros, 0xFF, counter) |

Consistent reading (ours, inferred, not confirmed by firmware): bit6 is
mandatory and carries no byte; bit1 carries the **packet counter** (increments
by the number of haptic frames sent); bits 2–5 and 7 each carry a byte that
DS5Dongle labels "AudioBufferLength" (bit5, bit4) and guesses as volume
registers for the others ("guess but no work"). bit0 = controller microphone
enable (DS5Dongle sets it when the host opens the mic; it does not add a byte
in their code, so either it carries none or they rely on zero-init).
`AudioBufferLength` default 48 in DS5Dongle; ds5-bt-haptics exposes 16..127.
Our measurement: 127 together with 3 blocks of lead removed Bluetooth
dropouts, but we could not separate the two effects.

### 3.2 Haptic PCM packet 0x12

Signed 8-bit, interleaved stereo `L R L R …`, 3000 Hz. One frame = 64 bytes =
32 stereo samples = 10.667 ms. Channel 0 = left actuator, channel 1 = right
(matches USB channels 3/4). Double header 0xD2 = 128 bytes = 21.333 ms.
The 3 kHz figure was found by DS5Dongle's author with a tone generator over
USB (aliasing folds at multiples of 3 kHz) and confirmed by chiaki-ng, whose
PS5 stream delivers haptics as 3 kHz 16-bit stereo.

### 3.3 Speaker packet 0x13 / 0x16

Opus, encoder created at 48 kHz stereo, `OPUS_APPLICATION_AUDIO`, **CBR
160 kbit/s** so every 480-sample (10 ms nominal) frame is exactly 200 bytes.
The firmware consumes one such frame per haptic block (10.667 ms), so the
speaker stream actually plays at 45 kHz: LinuxAudio4Dualsense5 resamples each
512-input-frame block to 480 before encoding (period = 512/48000), and
dualsense_BT_audio decodes files at 45 kHz and labels the Opus stream 48 kHz.
Getting this wrong gives a periodic ~0.5 s stutter (clock mismatch), found via
btmon. The speaker must also be routed/unmuted via report 0x31 (see §5) and
the mic should be muted or the controller floods the link with mic reports.

### 3.4 Microphone

When the speaker/audio path is active the controller sends Opus-encoded mic
audio back inside its input reports: 48 kHz mono, 10 ms frames, 71 bytes per
frame (DS5Dongle `MIC_OPUS_SIZE`). Not implemented here.

## 4. Report 0x32, exact layout used by this project

```
off  value      meaning
 0   0x32
 1   seq<<4
 2   0x91       audio control sub-packet
 3   0x04       popcount(0x66)
 4   0x66       (| 0x01 to enable the mic)
 5   buf        buffer length (we send 127; protocol default 48)
 6   buf
 7   counter    += frames in this report, wraps at 256
 8   0x92|0xD2  haptic sub-packet, 0xD2 when two frames follow
 9   0x40       64 bytes per frame
10.. PCM        64 or 128 bytes int8 L R L R
138  CRC32 LE   over 0xA2 ‖ bytes 0..137
```

SAxense's variant (also works): `[2]=0x91 [3]=7 [4]=0xFE [5..9]=0,0,0,0,0xFF
[10]=counter [11]=0x92 [12]=64 [13..76]=PCM`.

DS5Dongle sends one 0x32 with mask 0x02 (and 0x03 when the mic is on) right
after connecting ("update_mic_status"). Not required for haptics; we never send
it and haptics work.

## 5. Report 0x31 fields that matter for audio

SetStateData starts at report byte 3 (47 bytes). Offsets below are **within
SetStateData**; add 3 for the BT report byte. From the Linux `hid-playstation`
driver and DS5Dongle's Wireshark dissector:

| off | field | notes |
|---|---|---|
| 0 | flags0 | 0x01 EnableRumbleEmulation, 0x02 UseRumbleNotHaptics (kernel: HAPTICS_SELECT), 0x04/0x08 trigger FFB, 0x10 AllowHeadphoneVolume, 0x20 AllowSpeakerVolume, 0x40 AllowMicVolume, 0x80 AllowAudioControl |
| 1 | flags1 | 0x01 mute LED, 0x02 AllowAudioMute/power-save, 0x04 lightbar, 0x08 reset lights, 0x10 player LEDs, 0x20 **AllowHapticLowPassFilter**, 0x40 **AllowMotorPowerLevel**, 0x80 AllowAudioControl2 |
| 2,3 | rumble right (light), left (heavy) | classic rumble emulation on the same actuators; mixes with/overrides the PCM stream |
| 4 | VolumeHeadphones | 0..0x7f |
| 5 | VolumeSpeaker | kernel: accepted range seems to be 0x3d..0x64 |
| 6 | VolumeMic | 0..0x40 |
| 7 | AudioControl | bits0-1 MicSelect, 0x04 echo cancel, 0x08 noise cancel, bits4-5 OutputPathSelect (0x30 = speaker, headphones muted; 0 = headphones), bits6-7 InputPathSelect |
| 9 | MuteControl | 0x10 mic mute (kernel), 0x80 HapticMute, power-save bits |
| 36 | MotorPowerLevel | low nibble TriggerMotorPowerReduction, high nibble RumbleMotorPowerReduction, x12.5 % each |
| 37 | AudioControl2 | bits0-2 SpeakerCompPreGain (kernel uses 2 = +6 dB), 0x08 beamforming |
| 38 | flags2 | 0x01 brightness, 0x02 fade animation, 0x04 EnableImprovedRumbleEmulation (kernel: COMPATIBLE_VIBRATION2, fw >= 2.21) |
| 39 | Haptic LPF | bit0 HapticLowPassFilter |
| 41-46 | lightbar setup, brightness, player LEDs, RGB | |

Speaker enable used by the BT speaker projects: flags0 = 0xA0, flags1 = 0x80,
VolumeSpeaker = 0x64, AudioControl = 0x30, AudioControl2 = 0x02.
The kernel driver creates the jack/audio objects only for USB and states
"Bluetooth audio is currently not supported".

## 6. Timing and link behaviour (measured here)

- Host side: Python `timerfd` sender held 10.68 ms cadence, 0 late ticks,
  hidraw `write()` 6–71 µs. PipeWire sink at `node.latency=512/48000`
  delivered exactly 512 frames per callback, max callback gap 10.69 ms.
- Despite that, the user felt brief dropouts in steady tones on two different
  paths (direct Python and PipeWire). So the loss is on the Bluetooth link or
  in the controller, not in scheduling. WiFi on the combo chip was not
  associated at the time.
- Priming each burst with 3 silent reports back-to-back and setting the buffer
  byte to 127 (+32 ms latency) → 20 s steady tone with zero dropouts.
  dualsense_BT_audio independently "primes the controller with the first three
  packets"; chiaki-ng uses a 32 ms host-side jitter buffer.
- hidraw writes to a BT HID device never block: uhid queues the event and
  BlueZ sends it on the L2CAP interrupt channel. An overflowing uhid queue is
  dropped with a kernel warning, not an error to the writer.
- When the controller powers off, its `/sys/class/hidraw/hidrawN` disappears
  immediately; an open fd keeps returning ENODEV on write. On reconnect it
  comes back with a new HID id and hidraw number.
- Steam running in the background (it opens hidraw and writes 0x31) did not
  interfere with the 0x32 stream.
- Bandwidth: 142 B / 10.667 ms ≈ 13.3 kB/s outbound.

## 7. PipeWire specifics that bit us

- `pw-loopback -C <sink-node>` linked its capture stream to the default
  **source** (the headset microphone), not to the sink's monitor; the result
  was a silent mirror. Linking `<sink>:monitor_FL/FR` to our input ports with
  `pw-link` works and adds no node.
- With EasyEffects installed, applications play into `easyeffects_sink`;
  `pactl get-default-sink` still reports the hardware sink. Mirror the one
  the apps actually use.
- A `pw_stream` with `media.class=Audio/Sink`, direction INPUT, flags
  AUTOCONNECT|MAP_BUFFERS|RT_PROCESS appears as a normal sink and keeps being
  processed (with silence) while idle; idle detection must live in the sink.
- Writing the hidraw from the RT process callback worked without xruns
  (write ≤ 12 µs).

## 8. Open questions

- Exact semantics of the 0x11 mask bits and of the "AudioBufferLength" byte
  (units, whether 127 is honoured or clamped to 48).
- Whether the 4-bit sequence nibble matters when two writers (kernel/Steam
  with 0x31, us with 0x32) interleave. No symptom seen.
- Reports 0x33/0x34/0x35/0x37/0x38: never observed in any project.
- Clock drift between host graph clock and the controller's 3 kHz: not
  measured; no symptom in sessions of a few minutes.
- True end-to-end latency (audio in → actuator): not instrumented. Estimate
  ≈ 10.7 ms quantum + 32 ms lead + Bluetooth (~10–20 ms).
- DualSense Edge vs original DualSense: everything here was measured on the
  Edge; the projects above were developed on the original. Same layout works.

## 9. Sources (commit read)

| project | commit | date | URL |
|---|---|---|---|
| egormanga/SAxense (MPL-2.0; author asks for credit + link https://apps.sdore.me/SAxense) | 6fd1648e0b21 | 2026-01-29 | https://github.com/egormanga/SAxense |
| awalol/DS5Dongle (MPL-2.0) — `src/audio.cpp`, `src/utils.h`, `tools/wireshark_dualsense_setstate.lua` | 77d378effcda | 2026-10-28 | https://github.com/awalol/DS5Dongle |
| yyusvf/ds5-bt-haptics — `DS5BtHaptics/Protocol.cs`, `Streaming.cs` | 837848b12097 | 2026-10-25 | https://github.com/yyusvf/ds5-bt-haptics |
| Jindantesparda/dualsense_BT_audio — `src/ds5.h`, `src/ds5.cpp`, README | 10ff249c4046 | 2026-10-27 | https://github.com/Jindantesparda/dualsense_BT_audio |
| ebraminio/LinuxAudio4Dualsense5 — `src/ds5_membrane_sink.c`, `reverse_engineered/` | 54c543ad78e1 | 2026-06-30 | https://github.com/ebraminio/LinuxAudio4Dualsense5 |
| sendement/dualsense-haptics — `bt_hid_proxy.py`, `tests/test_bt_protocol.py` | 105bfd4d2f5a | 2026-10-25 | https://github.com/sendement/dualsense-haptics |
| jpbjesus/dualwave (not read in depth) | c9d5c9938da9 | 2026-11-03 | https://github.com/jpbjesus/dualwave |
| chiaki-ng PR 858 "Support DualSense haptics over Bluetooth" | — | 2026 | https://github.com/chiaki-ng/chiaki-ng/pull/858 |
| nondebug/dualsense (report descriptors) | — | — | https://github.com/nondebug/dualsense |
| Linux `drivers/hid/hid-playstation.c` | master, 2026-11 | — | https://github.com/torvalds/linux/blob/master/drivers/hid/hid-playstation.c |
| hifihedgehog/PadForge wiki (DualShock 4 BT audio: SBC in report 0x17, 32 kHz) | — | — | https://github.com/hifihedgehog/PadForge/wiki/Controller-Audio |
| Notebookcheck, "DualSense PS5 controller gains haptics over Bluetooth on PC" (DSX v3.2 Beta 01) | — | 2026-06-13 | https://www.notebookcheck.net/DualSense-PS5-controller-gains-haptics-over-Bluetooth-on-PC-closing-gap-with-Xbox.1321356.0.html |
| PCGamingWiki Controller:DualSense | — | — | https://www.pcgamingwiki.com/wiki/Controller:DualSense |
