#!/bin/bash
# Install racecar udev rules + modprobe blacklists, then reload.
# Idempotent: re-installs every run (install is cheap), but only regenerates
# initramfs when the blacklist content changed (that step takes ~30 s).
set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

RULES_SRC="${SCRIPT_DIR}/udev/99-racecar.rules"
RULES_DST="/etc/udev/rules.d/99-racecar.rules"

# Module blacklist: prevents hid_nintendo from claiming the EasySMX KC-8236
# (spoofs Switch Pro VID:PID 057e:2009). With hid_nintendo gone, the
# controller's firmware downgrades to Xbox 360 mode (2f24:016d) and binds
# to `xpad`, giving us /dev/input/js0 + correct button mapping. See the
# rationale in the .conf header.
MODPROBE_SRC="${SCRIPT_DIR}/modprobe.d/blacklist-hid-nintendo.conf"
MODPROBE_DST="/etc/modprobe.d/blacklist-hid-nintendo.conf"

if [[ ! -f "${RULES_SRC}" ]]; then
    echo "Missing ${RULES_SRC}" >&2
    exit 1
fi
if [[ ! -f "${MODPROBE_SRC}" ]]; then
    echo "Missing ${MODPROBE_SRC}" >&2
    exit 1
fi

# Install the udev rules (cheap).
sudo install -m 0644 "${RULES_SRC}" "${RULES_DST}"
sudo udevadm control --reload-rules
sudo udevadm trigger

# Install the modprobe blacklist. The kernel reads /etc/modprobe.d/ on
# boot, BUT hid_nintendo may be loaded from the initramfs before that —
# so when the blacklist changes, we also need to regenerate the initramfs.
INITRAMFS_NEEDED=0
if ! sudo cmp -s "${MODPROBE_SRC}" "${MODPROBE_DST}" 2>/dev/null; then
    sudo install -m 0644 "${MODPROBE_SRC}" "${MODPROBE_DST}"
    INITRAMFS_NEEDED=1
fi

if [[ $INITRAMFS_NEEDED -eq 1 ]]; then
    # Unload the running module if present so the change takes effect
    # this boot too (otherwise blacklist only applies next reboot).
    if lsmod | grep -q '^hid_nintendo'; then
        echo "  Unloading running hid_nintendo module..."
        sudo modprobe -r hid_nintendo 2>/dev/null || true
    fi
    if command -v update-initramfs >/dev/null; then
        echo "  Regenerating initramfs (~30s) to bake in the blacklist..."
        sudo update-initramfs -u
    fi
fi

echo "Installed ${RULES_DST} and ${MODPROBE_DST}; symlinks should appear under /dev/."
echo "If the gamepad was just plugged in, unplug + replug it once for the change to take effect."
