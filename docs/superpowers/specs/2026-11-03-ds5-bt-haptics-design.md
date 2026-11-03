# DualSense haptics over Bluetooth on Linux — design

Date: 2026-11-03
Status: approved (approach 1)

## Goal

Make the DualSense / DualSense Edge voice-coil ("HD") haptics work over
Bluetooth on this Arch Linux machine, with latency low enough for games and
music, and expose it as an ordinary PipeWire audio sink so any application can
drive it.

Out of scope for this spec (future phases, separate specs):

- Automatic game routing (a virtual 4-channel sink that impersonates the wired
  DualSense USB audio device so Proton/SDL games pick it up by name).
- Speaker / headphone-jack audio over Bluetooth (Opus in reports 0x36/0x39).
- Microphone input.
- GUI.

## Background (research result)

The controller accepts raw haptic PCM over Bluetooth in HID output report
`0x32` (142 bytes). No codec. Format: signed 8-bit, interleaved stereo
(left actuator, right actuator), 3000 Hz. One 64-byte block = 32 frames =
10.667 ms. A report may carry one block (header `0x92`) or two (`0xD2`,
21.333 ms). Layout, taken from the DS5Dongle firmware and confirmed by SAxense,
chiaki-ng PR 858, ds5-bt-haptics and KytyPS5:

```
off  value        meaning
 0   0x32         report id
 1   seq<<4       4-bit sequence number in the high nibble, low nibble 0
 2   0x91         sub-packet 0x11 (audio control), "sized" bit set
 3   0x04         popcount of byte 4
 4   0x66         flags: bit0 mic on, bit5 buffer-length valid, bit6 mandatory
 5   48           audio buffer length (samples)
 6   48           audio buffer length (samples)
 7   counter      packet counter, += frames in this report
 8   0x92 | 0xD2  sub-packet 0x12 (haptic PCM); 0xD2 = two frames follow
 9   64           bytes per frame
10.. int8 PCM     64 or 128 bytes, L R L R ...
138  crc32 LE     CRC-32 over the byte 0xA2 followed by bytes 0..137
```

The CRC is the standard zlib CRC-32 of `b"\xA2" + report[0:138]`, identical
to the one the kernel uses for report 0x31.

Pacing must match the controller's 3 kHz consumption. Its buffer is about 48
samples, so a late report is heard as a dropout and a burst as a glitch.
Projects that work pace with a monotonic timer and send exactly one report per
period. The main source of latency in existing Linux attempts was audio
capture through pipes, not Bluetooth; a native PipeWire node with a
512/48000 quantum (10.667 ms) avoids that.

Steam and the kernel driver both write report 0x31 (LEDs, triggers, classic
rumble). hidraw is not exclusive on Linux, so they coexist with our 0x32
stream. The only known interaction is that classic rumble sent while haptics
play is mixed/overridden by the controller.

## Architecture

Two deliverables sharing one protocol definition.

```
                 ┌──────────────────────────┐
 any app ──PW──► │ ds5-haptics-sink (C)     │──0x32──► /dev/hidrawN ──BT──► controller
                 │ PipeWire Audio/Sink node │
                 └──────────────────────────┘

 ds5bt (Python CLI) ─────────────────────0x32──► /dev/hidrawN
   tone / sweep / wav / stdin, timing stats
```

### 1. `ds5bt` — Python test and diagnostics tool

Package `ds5bt/` with a console entry point `ds5bt`.

- `ds5bt/protocol.py` (pure, unit-tested)
  - `crc32_sony(data: bytes) -> int` — zlib CRC-32 over `0xA2 ‖ data`.
  - `build_haptics_report(frames: bytes, seq: int, counter: int, mic: bool=False) -> bytes`
    — 142-byte report 0x32; `frames` is 64 or 128 bytes of int8 PCM.
  - `build_state_report(seq, *, haptic_lpf: bool | None, rumble_power_reduction: int | None) -> bytes`
    — 78-byte report 0x31 touching only the haptic low-pass-filter bit
    (byte 39 bit 0, enable flag1 bit 5) and motor power level (byte 36,
    enable flag1 bit 6). Used as an optional tuning knob, off by default.
  - `find_hidraw() -> list[str]` — sysfs scan for
    `/sys/bus/hid/devices/0005:054C:{0CE6,0DF2}.*/hidraw/hidraw*`; returns
    `/dev/hidrawN` paths. Bluetooth bus only (0005).
  - Constants: `RATE = 3000`, `FRAME_BYTES = 64`, `FRAME_SAMPLES = 32`.
- `ds5bt/stream.py`
  - `HapticStreamer(path, double=False)` — opens the hidraw node, owns
    seq/counter, sends with `os.timerfd_create(CLOCK_MONOTONIC)` at
    10,666,667 ns (or 21,333,333 ns for double frames). `run(source)`
    pulls one block per tick from `source` (callable returning 64 or 128
    bytes, or `None` to stop). `mlockall` best-effort. Records stats:
    reports sent, late ticks (timerfd expirations > 1), max write time, max
    gap. Closes with four silent reports so the coils settle.
- `ds5bt/sources.py`
  - `tone(hz, seconds, channel: "left"|"right"|"both", gain)`
  - `sweep(f0, f1, seconds, gain)`
  - `wav(path, gain)` — any sample rate/channels; resampled to 3 kHz with
    `scipy.signal.resample_poly`, stereo mapped L→left actuator, R→right;
    mono duplicated.
  - `stdin_s8()` — raw signed 8-bit stereo 3 kHz from stdin (pipe-compatible
    with SAxense usage).
- `ds5bt/cli.py` — `ds5bt info | tone | sweep | wav | stdin`, common flags
  `--device`, `--double`, `--gain`, `--lpf on|off`, `--stats`.

Dependencies: Python ≥ 3.13 (for `os.timerfd_*`), numpy, scipy. No hidapi.

### 2. `ds5-haptics-sink` — native PipeWire sink (C)

Single file `sink/ds5_haptics_sink.c`, built with `libpipewire-0.3` and
zlib (for CRC). Modelled on LinuxAudio4Dualsense5's membrane sink.

- Node: `media.class=Audio/Sink`, `node.name=ds5_haptics`,
  `node.description=DualSense Haptics (BT)`, `device.icon-name=input-gaming`,
  format F32 planar/interleaved 2ch 48000 Hz, `node.latency=512/48000`,
  `audio.position=FL,FR`.
- Process callback (every 512 frames = one 10.667 ms block):
  1. Per channel: 4th-order Butterworth low-pass at 1200 Hz (two cascaded
     biquads, state kept across callbacks) as anti-alias filter.
  2. Decimate 16:1 (take every 16th filtered sample) → 32 frames.
  3. Apply gain (default 1.0, env `DS5_HAPTICS_GAIN` or `--gain`), clip to
     [-1, 1], convert to int8 (`lrintf(x * 127)`).
  4. If the block and the previous ~47 blocks (≈500 ms) are all silent, skip
     the write (idle). Otherwise build report 0x32 (single frame) and
     `write()` it to the hidraw fd.
  Callbacks that arrive while there is no controller discard audio.
- Device handling: on start and every 1 s while disconnected, glob the sysfs
  path above and open the first match `O_WRONLY|O_NONBLOCK`. On `write()`
  error other than `EAGAIN`, log, close, and return to searching. The node
  stays present so applications keep their routing.
- Optional one-shot report 0x31 on connect when `--lpf on|off` or
  `--power-reduction N` is given; otherwise the controller's defaults are
  left alone.
- Logging to stderr: connect/disconnect, write errors, once-per-minute stats
  (blocks sent, idle skips, write errors, max write µs).
- `Makefile`: `make`, `make install` (to `~/.local/bin`), `make service`
  (installs and enables `systemd/ds5-haptics-sink.service`, a user unit with
  `After=pipewire.service`, `Restart=on-failure`).

Clock drift between the PipeWire graph clock and the controller's 3 kHz
crystal is accepted for v1 (expected in the tens of ppm; the controller's
buffer absorbs it for minutes and resynchronises on a dropout). If audible,
v2 can add a drift estimator that drops or duplicates one sample per block.

## Data flow summary

```
app audio (any rate) → PipeWire resample → F32 48 kHz 2ch, 512 frames
  → LPF 1.2 kHz → ÷16 → gain/clip → int8 ×64 → report 0x32 (142 B)
  → write(hidraw) → BlueZ uhid → L2CAP interrupt channel → controller coils
```

Outbound bandwidth ≈ 13.3 kB/s at one report per 10.667 ms.

## Error handling

- No controller: `ds5bt` exits with a clear message listing what it looked
  for; the sink logs and keeps retrying.
- Controller disconnects mid-stream: `ds5bt` stops with stats; the sink
  re-scans.
- hidraw permission denied: message pointing at the udev `uaccess` rule for
  054C:0DF2 / 0CE6 (already present on this machine).
- Late ticks: counted and reported, not hidden.

## Testing

Unit (pytest, `tests/test_protocol.py`):

- CRC equals `zlib.crc32(b"\xA2" + data)`.
- Report 0x32 single frame: length 142, bytes 0–9 exactly as the table
  above, PCM at 10..73, zeros 74..137, CRC at 138..141.
- Report 0x32 double frame: byte 8 = 0xD2, PCM at 10..137.
- Counter and sequence wrap (seq mod 16, counter mod 256).
- Report 0x31 LPF/power-reduction builder sets only the intended bytes and
  flags, length 78, valid CRC.
- `find_hidraw` parsing against a fake sysfs tree in `tmp_path`.
- Source generators: tone block lengths, channel isolation (right channel
  all zero for `left`), WAV resampling output length.

Hardware (manual, on the DualSense Edge connected over Bluetooth):

1. `ds5bt tone 80 --sec 2 --ch left` → vibration in left grip only; same
   for right; `--ch both` → both. Stats show 0 late ticks.
2. `ds5bt sweep 40 1000 --sec 6` → pitch rises, no stutter.
3. `ds5bt wav some.wav` → recognisable content.
4. `ds5-haptics-sink` running: sink appears in `pactl list sinks short`;
   `pw-play --target ds5_haptics file.wav` plays as haptics; idle skip
   observed in log after silence; power the controller off and on → sink
   reconnects without restart.
5. Rough latency check: `pw-loopback` from a click track to both the headset
   and the haptics sink; click and pulse should feel simultaneous.

## Credits

Protocol derived from public reverse-engineering work: SAxense by egormanga
(https://apps.sdore.me/SAxense, MPL-2.0), DS5Dongle by awalol (MPL-2.0),
chiaki-ng PR 858, ds5-bt-haptics, KytyPS5, LinuxAudio4Dualsense5. This
project is an independent implementation of the documented byte layout; no
source code is copied from those projects.
