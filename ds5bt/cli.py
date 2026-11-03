"""ds5bt — DualSense haptics over Bluetooth test tool."""
from __future__ import annotations

import argparse
import os
import sys

from . import protocol, sources
from .stream import HapticStreamer


def _try_realtime() -> bool:
    try:
        os.sched_setscheduler(0, os.SCHED_FIFO, os.sched_param(20))
        return True
    except (PermissionError, OSError):
        return False


def _device(args) -> str:
    if args.device:
        return args.device
    devs = protocol.find_hidraw()
    if not devs:
        sys.exit("ds5bt: no DualSense over Bluetooth. Looked for "
                 "/sys/bus/hid/devices/0005:054C:{0CE6,0DF2}.*/hidraw. "
                 "USB-connected controllers are ignored on purpose.")
    return devs[0]


def cmd_info(args) -> int:
    devs = protocol.find_hidraw()
    if not devs:
        print("no DualSense connected over Bluetooth")
        return 1
    for dev in devs:
        name = "?"
        try:
            with open(f"/sys/class/hidraw/{os.path.basename(dev)}/device/uevent") as f:
                for line in f:
                    if line.startswith("HID_NAME="):
                        name = line.strip().split("=", 1)[1]
        except OSError:
            pass
        access = "rw" if os.access(dev, os.R_OK | os.W_OK) else "NO ACCESS"
        print(f"{dev}\t{name}\t{access}")
    return 0


def _run(args, source) -> int:
    dev = _device(args)
    try:
        st = HapticStreamer(dev, double=args.double, lead=args.lead, buffer_len=args.buffer)
    except PermissionError:
        sys.exit(f"ds5bt: {dev}: permission denied. Needs the udev uaccess rule for "
                 "054C:0DF2 / 054C:0CE6 hidraw nodes, or rw access to the device.")
    if args.lpf is not None or args.power_reduction is not None:
        rep = protocol.build_state_report(st.seq, haptic_lpf=None if args.lpf is None else args.lpf == "on",
                                          rumble_power_reduction=args.power_reduction)
        os.write(st.fd, rep)
        st.seq = (st.seq + 1) & 0x0F
    rt = _try_realtime()
    print(f"ds5bt: {dev}, {'double' if args.double else 'single'} frame reports, lead {args.lead}, "
          f"buffer {args.buffer}, realtime {'on' if rt else 'off'}", file=sys.stderr)
    try:
        st.run(source)
    except OSError as e:
        print(f"ds5bt: write failed: {e} (controller disconnected?)", file=sys.stderr)
    except KeyboardInterrupt:
        pass
    print(st.stats.summary(), file=sys.stderr)
    return 0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="ds5bt", description="DualSense haptics over Bluetooth")
    ap.add_argument("--device", help="hidraw path (default: first Bluetooth DualSense)")
    ap.add_argument("--double", action="store_true", help="two blocks per report (21.3 ms cadence)")
    ap.add_argument("--gain", type=float, default=1.0)
    ap.add_argument("--lead", type=int, default=3, metavar="N",
                    help="prime the controller with N silent reports: N x 10.7 ms latency buys N x 10.7 ms "
                         "tolerance against Bluetooth stalls (default 3; 0 = lowest latency, audible dropouts)")
    ap.add_argument("--buffer", type=int, default=127, metavar="16-127",
                    help="controller-side buffer length byte (default 127; protocol default is 48)")
    ap.add_argument("--lpf", choices=["on", "off"], help="set the controller's haptic low-pass filter first")
    ap.add_argument("--power-reduction", type=int, choices=range(8), metavar="0-7",
                    help="rumble motor power reduction, x12.5%% steps")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("info", help="list Bluetooth DualSense hidraw nodes")
    t = sub.add_parser("tone", help="sine tone")
    t.add_argument("hz", type=float)
    t.add_argument("--sec", type=float, default=2.0)
    t.add_argument("--ch", choices=["left", "right", "both"], default="both")
    s = sub.add_parser("sweep", help="exponential sweep on both actuators")
    s.add_argument("f0", type=float)
    s.add_argument("f1", type=float)
    s.add_argument("--sec", type=float, default=6.0)
    w = sub.add_parser("wav", help="play a WAV file (L→left, R→right)")
    w.add_argument("path")
    sub.add_parser("stdin", help="raw s8 stereo 3 kHz from stdin")
    args = ap.parse_args(argv)

    if args.cmd == "info":
        return cmd_info(args)
    if args.cmd == "tone":
        src = sources.tone(args.hz, args.sec, args.ch, args.gain)
    elif args.cmd == "sweep":
        src = sources.sweep(args.f0, args.f1, args.sec, args.gain)
    elif args.cmd == "wav":
        src = sources.wav(args.path, args.gain)
    else:
        src = sources.stdin_s8()
    return _run(args, src)
