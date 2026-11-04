# How Proton games find the DualSense haptics endpoint — research notes

Date: 2026-11-04. Sources: Proton's Wine (ValveSoftware/wine, branches
`proton_10.0` and `experimental_10.0`, files `dlls/winebus.sys/bus_udev.c`,
`dlls/winepulse.drv/pulse.c`, `dlls/mmdevapi/devenum.c`), Proton issue #5900,
CachyOS/proton-cachyos issue #296, NotProton PR #203, GE-Proton 11-4 notes,
`snooptheone/ds5bridge` (v0.1.0, docs/PROTOCOL.md), `iranran666/dse-haptics`
(docs/HOW-IT-WORKS.md). Verified against the binaries shipped in this
machine's Proton Experimental (bleeding-edge 11.0, 2026-11-03) and
GE-Proton 10-34 (`strings` show `get_container_id_for_usb_udev_device` in
`winebus.so` and `get_container_id` + `sysfs.path` in `winepulse.so`).

## 1. What a native-haptics game does on Windows

The wired DualSense is a composite USB device: HID interface + USB Audio
Class 4-channel output (ch 1–2 speaker/headphones, ch 3–4 left/right haptic
actuator) + 2-channel mic. Games (Sony's libScePad, and in-house code in
FF7R, FFXIV, Deathloop, Ghostwire, Cyberpunk, AC Shadows …) open the HID
device for input/triggers and a WASAPI render endpoint for haptics. They pick
the endpoint by one of:

1. friendly name containing "Wireless Controller" (the Windows name of the
   DualSense audio endpoint): FFXIV, FF7R;
2. `DEVPKEY_Device_ContainerId` of the endpoint equal to the HID device's
   `DEVPKEY_Device_BaseContainerId`: Deathloop, Ghostwire, Cyberpunk, AC
   Shadows, libScePad titles (Returnal, Death Stranding, Pragmata);
3. SetupAPI enumeration of audio devices sharing the HID's container
   (Deathloop with XAudio2).

Games also expect the endpoint to be 4-channel (Windows ships it stereo;
users must set "Quadraphonic"). The controller must be in native haptics
mode: a `0x02`/`0x31` output report with the rumble-emulation flags cleared;
anything that sends classic rumble (kernel FF, Steam Input, Wine DirectInput
rumble) flips it back.

## 2. How Proton implements the matching (from source)

`winebus.sys` (`bus_udev.c`, `udev_add_device`):

```c
if (desc.bus_type == BUS_TYPE_USB) get_container_id_for_usb_udev_device(dev, &desc);
```

```c
/* parent = udev usb_device of the HID node */
desc->bus_container_id.Data1 = MAKELONG(vid, pid);
desc->bus_container_id.Data2 = bus_num;      /* udev BUSNUM */
desc->bus_container_id.Data3 = dev_num;      /* udev DEVNUM */
memcpy(desc->bus_container_id.Data4, &init_time, 8);   /* udev USEC_INITIALIZED */
```

A **Bluetooth** HID device (`bus == BUS_BLUETOOTH`) gets no container id.

`winepulse.drv` (`pulse.c`): for each PulseAudio/PipeWire sink it reads the
proplist key `sysfs.path`, resolves `/sys<path>` with udev, walks up to the
`usb_device` parent and builds the identical GUID
(`create_usb_dev_container_id`). It also reads `device.bus`,
`device.vendor.id`, `device.product.id`. `mmdevapi` exposes that GUID as
`DEVPKEY_Device_ContainerId`.

Consequences:

- Over USB everything lines up without configuration in Proton ≥ 10.0-4
  ("fixed haptics support for DualSense controllers"), provided the sink is
  4-channel and named "Wireless Controller" (Proton names it; WirePlumber
  rules in the community fix the UCM profile split and the channel count).
- Over Bluetooth, no virtual PipeWire sink can satisfy rule 2: the HID side
  has no container id, and a virtual sink has no USB parent for
  `sysfs.path`. Only rule 1 (name) can be faked, which covers FFXIV/FF7R-style
  games and nothing that uses libScePad.
- `winepipewire.drv` (CachyOS's native PipeWire driver) initially had no
  container id at all (issue #296); `PROTON_USE_PIPEWIRE=0` falls back to
  winepulse.
- GE-Proton 11 reworked Sony controller handling (`PROTON_SONY_*` options,
  "finalized DualSense/Edge haptics, speaker, hotplug"); a Pico-based USB
  dongle reportedly stopped working from GE 11-3 while Proton-CachyOS still
  worked. ds5bridge's author reports GE-Proton 11-1 and Proton Experimental
  11.0-100 working with a clean prefix.

## 3. The way through: a virtual USB device (ds5bridge)

ds5bridge runs as root, waits for a Bluetooth DualSense/Edge (udev rule on
`0005:054C:{0CE6,0DF2}`), and attaches a virtual USB device to the kernel's
`vhci_hcd` by writing `"port sockfd devid speed"` to
`/sys/devices/platform/vhci_hcd.0/attach` with one end of an `AF_UNIX`
socketpair; it then speaks the USB/IP device protocol on the other end
(no `usbip` tool, no TCP). The virtual device carries the real pad's USB
descriptors (HID + UAC1 4-ch out + 2-ch mic), so `hid-playstation` and
`snd-usb-audio` bind to it, PipeWire creates
`alsa_output.usb-Sony_Interactive_Entertainment_DualSense…`, and Proton's
USB container logic works unchanged. `dummy_hcd` (configfs gadget loopback)
was rejected upstream because it has no isochronous endpoints.

Bridging: BT input `0x31` → USB input `0x01`; USB output `0x02` → BT `0x31`
(+CRC) for triggers/LEDs; USB iso OUT ch 3–4 → 385-tap Kaiser FIR (fc
1250 Hz) → ÷16 → int8 → BT `0x39` (two 64-byte blocks per 21.33 ms, pacer
with 2-packet prebuffer, buffer byte 100); ch 1–2 → Opus 160 kbps CBR →
same `0x39`. The real pad's feature reports (`0x05` calibration, `0x09`
pairing/MAC with the locally-administered bit flipped, `0x20` firmware) are
served to the host. An HCI QoS Setup (5 ms) raises the pad's input rate from
~70/s to ~300/s on the author's Intel adapter. The real pad's hidraw/evdev
become root-only (udev rule, only while `vhci_hcd` is loaded) so Steam sees
one controller. Steam Input must be disabled per game.

Their buffering numbers agree with our phase-1 measurement: without lead the
controller drops out on Bluetooth stalls; they prebuffer 2 packets (42 ms)
and use buffer byte 100; we use 3 blocks (32 ms) and 127.

## 4. Status on this machine

Prerequisites present: kernel 7.2.11 with `CONFIG_USBIP_VHCI_HCD=m` (and
`USB_DUMMY_HCD=m`), opus 1.6.1, WirePlumber 0.5, ALSA UCM
`USB-Audio/Sony/DualSense-PS5.conf`, Proton Experimental 11.0 bleeding edge,
GE-Proton 10-34, Proton-CachyOS. Installed games with native DualSense
haptics: Cyberpunk 2077, FINAL FANTASY XIV, Lies of P, Monster Hunter Wilds.

ds5bridge v0.1.0 built from source and its full test suite passed here
(unit, golden packets, USB/IP protocol, lifecycle, multipad, route, tools,
options, install, packaging, wireplumber). Hardware validation results are
recorded in the README as they happen.
