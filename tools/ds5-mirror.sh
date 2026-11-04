#!/usr/bin/env bash
# Mirror a PipeWire node's output into the DualSense haptic channels.
#
#   tools/ds5-mirror.sh [SOURCE_NODE]   link SOURCE_NODE's monitor into the haptics
#   tools/ds5-mirror.sh --off           remove those links
#
# Target, in order of preference:
#   1. the "Wireless Controller" 4-channel sink of the virtual USB pad that ds5bridge
#      creates: monitor FL/FR -> playback_RL/RR (channels 3-4 are the actuators)
#   2. phase 1's ds5_haptics sink: monitor FL/FR -> playback_FL/FR
# SOURCE_NODE defaults to easyeffects_sink when it exists, else the default sink.
set -euo pipefail

find_target() {
    # node.name of the sink whose description is "Wireless Controller", if any
    local name
    name=$(pw-dump 2>/dev/null | python3 -c '
import json, sys
for o in json.load(sys.stdin):
    p = o.get("info", {}).get("props", {})
    if p.get("media.class") == "Audio/Sink" and p.get("node.description") == "Wireless Controller":
        print(p.get("node.name")); break
')
    if [ -n "$name" ]; then
        echo "$name RL RR"
        return
    fi
    if pw-link -i 2>/dev/null | grep -q '^ds5_haptics:playback_FL$'; then
        echo "ds5_haptics FL FR"
        return
    fi
    echo "ds5-mirror: no haptics sink found (neither a \"Wireless Controller\" sink from ds5bridge nor ds5_haptics)" >&2
    exit 1
}

default_source() {
    if pw-link -o 2>/dev/null | grep -q '^easyeffects_sink:monitor_FL$'; then
        echo easyeffects_sink
    else
        pactl get-default-sink
    fi
}

read -r target rl rr < <(find_target)

if [ "${1:-}" = "--off" ]; then
    for l in $(pw-link -l 2>/dev/null | grep -B1 "^  |-> ${target}:playback_" | grep -v '^  |' | grep -v '^--'); do
        for ch in "$rl" "$rr"; do pw-link -d "$l" "${target}:playback_${ch}" 2>/dev/null || true; done
    done
    echo "ds5-mirror: links into ${target} removed"
    exit 0
fi

src=${1:-$(default_source)}
pw-link "${src}:monitor_FL" "${target}:playback_${rl}" 2>/dev/null || true
pw-link "${src}:monitor_FR" "${target}:playback_${rr}" 2>/dev/null || true
echo "ds5-mirror: ${src}:monitor_FL/FR -> ${target}:playback_${rl}/${rr}"
pw-link -l | grep -A1 "^${target}:playback_${rl}$" | head -2
