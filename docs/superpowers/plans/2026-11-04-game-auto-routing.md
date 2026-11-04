# Game Auto-Routing (ds5bridge integration) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Native-haptics Proton games get haptics and adaptive triggers from the DualSense Edge over Bluetooth, by installing and validating ds5bridge on this machine and integrating it with the phase-1 tools.

**Architecture:** ds5bridge (upstream, system package) presents the Bluetooth pad as a virtual USB DualSense via `vhci_hcd`; Proton's unchanged USB ContainerId logic links HID and audio. This repo adds a mirror helper that links any node's monitor into the virtual pad's haptic channels, a 4-channel test WAV, docs, and records the validation.

**Tech Stack:** ds5bridge 0.1.0 (C, GPL-3), makepkg/pacman, WirePlumber 0.5, pw-link, Python (test WAV), Proton Experimental / GE-Proton 10 / Proton-CachyOS.

Spec: `docs/superpowers/specs/2026-11-04-game-auto-routing-design.md`

---

### Task 1: Package and install ds5bridge

- [ ] **Step 1: Build the package (no root)** — done by the agent:
  `makepkg -f` with upstream `packaging/arch/PKGBUILD` (tag v0.1.0) in a scratch dir; `check()` runs the upstream test suite.
- [ ] **Step 2: Install (user runs, needs password):**
  ```bash
  sudo pacman -U /path/to/ds5bridge-0.1.0-1-x86_64.pkg.tar.zst
  systemctl --user restart wireplumber
  ```
- [ ] **Step 3: Verify install**
  ```bash
  lsmod | grep vhci_hcd; ls /sys/devices/platform/vhci_hcd.0/attach
  ls /usr/lib/udev/rules.d/7[12]-ds5bridge*.rules /usr/share/wireplumber/wireplumber.conf.d/90-dualsense.conf
  systemctl status ds5bridge --no-pager   # inactive until a pad connects (udev-started)
  ```

### Task 2: Virtual pad on the Edge

- [ ] **Step 1: Power the Edge on (Bluetooth).** Then:
  ```bash
  journalctl -u ds5bridge -n 40 --no-pager
  lsusb | grep -i 054c
  pactl list sinks short | grep -i -E "wireless|sony|dualsense"
  pactl list sinks | grep -A30 "Wireless Controller" | grep -E "Name|Description|Sample|Channel Map|State"
  ls -la /dev/hidraw*; ls /sys/bus/hid/devices/ | grep 054C
  ```
  Expected: log shows pad detected, feature reports read, "attached to vhci_hcd port N", QoS status; `lsusb` lists a Sony 054c:0ce6 device; a sink named "Wireless Controller", s16le 4ch 48000Hz, FL FR RL RR; two HID devices (0005:… root-only, 0003:… normal).
- [ ] **Step 2: Haptics through the virtual pad, no game**
  ```bash
  python3 tools/make_test_wav.py --4ch /tmp/t4.wav
  pw-play --target "$(pactl list sinks short | awk '/Wireless|DualSense/{print $2; exit}')" /tmp/t4.wav
  ```
  Expected: same L/R/rising pattern felt; `journalctl -u ds5bridge` shows "bt audio: ~46.9 packets/s".

### Task 3: Mirror helper and test WAV option

**Files:**
- Create: `tools/ds5-mirror.sh`
- Modify: `tools/make_test_wav.py` (add `--4ch`: content on channels 3–4 only, silence on 1–2)

- [ ] **Step 1: `tools/make_test_wav.py --4ch`**: write 4-channel S16 48 kHz; channels 0–1 silent; channel 2 = left pattern, channel 3 = right pattern (same signals as the stereo file).
- [ ] **Step 2: `tools/ds5-mirror.sh [SOURCE_NODE]`**: finds the sink whose `node.description` is "Wireless Controller" (falls back to `ds5_haptics` when ds5bridge is not running), links `SOURCE_NODE:monitor_FL → <sink>:playback_RL` and `FR → RR` (or `FL/FR` for `ds5_haptics`), `--off` unlinks. Default SOURCE_NODE = `easyeffects_sink` if present, else the default sink.
- [ ] **Step 3: Verify**: `tools/ds5-mirror.sh` then `pw-play test.wav` to the headset → haptics felt; `pw-link -l` shows the two links; `tools/ds5-mirror.sh --off` removes them.
- [ ] **Step 4: Commit** `git add tools && git commit -m "feat(tools): ds5-mirror helper and 4-channel test WAV"`

### Task 4: Game validation (manual)

For each game: Steam → game → Properties → Controller → "Disable Steam Input". Launch, play a minute, note haptics and triggers. Then record Proton build.

- [ ] Cyberpunk 2077 (ContainerId matching) on Proton Experimental
- [ ] FFXIV (name matching)
- [ ] Lies of P
- [ ] One of them on GE-Proton 10-34 and Proton-CachyOS
- [ ] Mid-session power cycle of the pad: virtual pad gone after 3 s, back on reconnect, triggers replayed (`journalctl -u ds5bridge`)
- [ ] If a game fails: `PROTON_LOG=1 WINEDEBUG=+mmdevapi,+hid %command%`, look for `ContainerId`, `AudioClient_Create`, `client_Initialize` 4 channels.

### Task 5: Docs

- [ ] `docs/research/2026-11-04-proton-dualsense-matching.md` (facts from Wine source, ds5bridge mechanism, results)
- [ ] README "Games (ds5bridge)" section + results table; note phase-1 sink vs ds5bridge exclusivity and rollback
- [ ] Commit; update memory.
