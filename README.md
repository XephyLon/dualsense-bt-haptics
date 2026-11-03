# DualSense haptics over Bluetooth (Linux)

HD haptics ("voice-coil" rumble) on a Sony DualSense or DualSense Edge
**over Bluetooth**, on Linux. No cable, no dongle.

Two tools:

| Tool | What it does |
|---|---|
| `ds5bt` (Python) | Test and diagnostics CLI: tones, sweeps, WAV files, raw stdin → controller. Prints pacing stats. |
| `ds5-haptics-sink` (C) | Native PipeWire sink **"DualSense Haptics (BT)"**. Anything routed to it plays on the actuators. Auto-reconnects when the controller drops. |

## How it works

The controller accepts raw haptic PCM in Bluetooth HID output report `0x32`:
signed 8-bit, interleaved stereo (left / right actuator), 3000 Hz, 32 frames
(10.667 ms) per report, CRC-32 seeded with the Bluetooth header byte `0xA2`.
Sony never exposed this on PC; the layout comes from community reverse
engineering (see Credits). The sink takes 48 kHz float audio from PipeWire,
low-passes at 1.2 kHz, decimates 16:1, converts to int8 and writes one report
per 512-frame cycle straight to `/dev/hidrawN`. The kernel `hid-playstation`
driver and Steam keep using report `0x31` for LEDs / triggers / classic
rumble; the two coexist.

## Requirements

- Linux with the `hid-playstation` driver (mainline since 5.16), BlueZ.
- Controller paired and connected over **Bluetooth** (USB is ignored on purpose:
  over USB the controller already exposes a 4-channel audio device).
- Read/write access to the controller's `/dev/hidrawN`. Arch's
  `game-devices-udev` ships `uaccess` rules for 054C:0CE6 / 054C:0DF2.
- `ds5bt`: Python ≥ 3.13, numpy, scipy.
- Sink: `libpipewire-0.3` headers, zlib, gcc, PipeWire ≥ 0.3.

## ds5bt

```bash
python3 -m ds5bt info                       # list Bluetooth DualSense hidraw nodes
python3 -m ds5bt tone 80 --sec 2 --ch left  # 80 Hz in the left grip
python3 -m ds5bt tone 80 --ch right
python3 -m ds5bt sweep 40 1000 --sec 6      # pitch glide on both
python3 -m ds5bt wav track.wav              # L → left actuator, R → right
ffmpeg -i x.mp3 -ac 2 -ar 3000 -f s8 - | python3 -m ds5bt stdin
```

Options: `--gain F`, `--lead N` / `--buffer N` (stall tolerance, see the sink
section; defaults 3 / 127), `--double` (two blocks per report, 21.3 ms
cadence), `--lpf on|off` (controller's own haptic low-pass filter),
`--power-reduction 0-7`, `--device /dev/hidrawN`. Every run ends with a stats
line:

```
192 reports in 2.05 s, late ticks 0, write errors 0, max write 6 us, max gap 10.68 ms
```

`late ticks` must stay 0; anything else means the sender fell behind.

## PipeWire sink

```bash
make -C sink                 # build
./sink/ds5-haptics-sink      # run (Ctrl-C to stop); --gain, --lpf, --verbose
make -C sink install         # → ~/.local/bin
make -C sink service         # systemd user unit, starts at login
```

Then:

```bash
pw-play --target ds5_haptics file.wav                    # play straight to the controller
pactl move-sink-input <id> ds5_haptics                   # route one app
./sink/ds5-haptics-sink --mirror easyeffects_sink        # feel everything that sink plays
```

`--mirror NODE` links NODE's monitor ports (`NODE:monitor_FL/FR`) into the
haptics sink with `pw-link`, retrying until NODE exists and re-checking every
10 s. Pick the node your apps actually play into: `pw-link -l | grep playback`
shows it. With EasyEffects that is `easyeffects_sink`, not the hardware sink.
Manual equivalent:

```bash
pw-link easyeffects_sink:monitor_FL ds5_haptics:playback_FL
pw-link easyeffects_sink:monitor_FR ds5_haptics:playback_FR
```

Do **not** use `pw-loopback -C <sink>` for this: its capture stream lands on
the default *source* (microphone), not the sink's monitor.

Other options: `--gain F` (or `DS5_HAPTICS_GAIN`), `--lead 0-8` and
`--buffer 16-127` (see *Stall tolerance* below), `--double` (two blocks per
report, 21.3 ms cadence, half the packet rate), `--lpf on|off`,
`--power-reduction 0-7`, `--verbose --stats-interval SEC` (prints sent /
non-silent / idle / errors / max process gap / input peak).

### Stall tolerance (`--lead`, `--buffer`)

The controller plays from a tiny buffer. If the Bluetooth link stalls for
longer than that buffer holds, you feel a dropout even though the host sent
every report on time. Both tools therefore **prime** each burst of audio with
`--lead N` silent reports sent back-to-back and ask for a larger buffer with
`--buffer 127`, so the controller runs N blocks ahead of real time. Each block
of lead costs 10.7 ms of latency and buys 10.7 ms of stall tolerance. Default
is `--lead 3 --buffer 127` (+32 ms); `--lead 0 --buffer 48` is the raw
protocol default and the lowest latency.

The sink idles (stops sending) after ~0.5 s of silence and resumes on signal.
If the controller powers off, the sink notices within a second (the hidraw
class device disappears), keeps the PipeWire node alive, and reopens the new
hidraw node when the controller comes back.

## Tested

2026-11-03, Arch Linux, kernel 7.2.11, BlueZ 5.87, PipeWire 1.6.9, MediaTek
MT7921/7922 Bluetooth 5.2 adapter, **DualSense Edge** (054C:0DF2) over
Bluetooth, Steam running in the background.

| Test | Result |
|---|---|
| `ds5bt tone 80 --ch left/right` | Felt, correct grip, clean separation |
| `ds5bt sweep 40 400` | Felt, pitch rises, no stutter |
| `pw-play --target ds5_haptics` through the sink | Felt L / R / both pattern as authored |
| `--mirror easyeffects_sink` while playing to the headset | Felt in sync with the headset |
| Power controller off / on while the sink runs | Sink reopened the new hidraw node, playback resumed |
| 20 s steady 70 Hz tone, `--lead 0 --buffer 48` | Occasional brief dropouts (Bluetooth stalls), host pacing perfect |
| 20 s steady 70 Hz tone, `--lead 3 --buffer 127` | No dropouts → made the default |
| Pacing | 0 late ticks, hidraw write 6–28 µs, cadence 10.68 ms |
| Sink process rate | 94 callbacks/s (512/48000), max process gap 10.69 ms, 358 reports for a 3 s clip + 0.5 s idle tail |

## Limitations

- 8-bit, 3 kHz. Content above ~1.2 kHz is filtered out before decimation.
- No automatic game routing yet. Games that drive DualSense haptics natively
  look for the wired USB audio device; a future phase can present a fake
  4-channel sink with that name. Until then, mirror the game's audio with
  `pw-loopback` or route the game to `ds5_haptics`.
- Classic rumble (e.g. from Steam Input) sent while haptics play is mixed in
  by the controller and can override it.
- Clock drift between PipeWire's graph clock and the controller's 3 kHz clock
  is not compensated; the controller's small buffer absorbs it and resyncs
  on a dropout.
- Speaker / headphone-jack audio over Bluetooth (Opus in reports 0x36/0x39)
  is documented in the spec but not implemented here.

## Credits

Protocol knowledge comes from public reverse-engineering work. This repo is
an independent implementation of the documented byte layout; no code was
copied from these projects.

- **SAxense** by egormanga — first public DualSense BT haptics POC.
  https://apps.sdore.me/SAxense · https://github.com/egormanga/SAxense
- **DS5Dongle** by awalol — full BT audio protocol in firmware form.
  https://github.com/awalol/DS5Dongle
- chiaki-ng PR 858, ds5-bt-haptics (yyusvf), KytyPS5, LinuxAudio4Dualsense5
  (ebraminio) — independent confirmations and the PipeWire-sink approach.
- Linux `hid-playstation` driver (Sony Interactive Entertainment) — report 0x31
  layout and CRC seed.

"PlayStation", "DualSense" and "DualSense Edge" are trademarks of Sony
Interactive Entertainment Inc. This project is not affiliated with Sony.
