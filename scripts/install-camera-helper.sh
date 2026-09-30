#!/bin/bash
set -euo pipefail
if [[ $EUID != 0 ]]; then
    echo 'Run with sudo bash scripts/install-camera-helper.sh' >&2
    exit 1
fi
app_user=${SUDO_USER:-base3}
[[ $app_user =~ ^[a-z_][a-z0-9_-]*$ ]] || { echo 'Invalid account name' >&2; exit 1; }
id "$app_user" >/dev/null
source_dir=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
station_file=$(dirname -- "$source_dir")/station.toml
for executable in /usr/bin/python3 /usr/sbin/wpa_cli /usr/sbin/iw /usr/sbin/ip /usr/sbin/modprobe /usr/bin/nmcli /usr/sbin/visudo; do
    [[ -x $executable ]] || { echo "Missing $executable" >&2; exit 1; }
done
rule_file=$(mktemp)
helper_file=$(mktemp)
trap 'rm -f "$rule_file" "$helper_file"' EXIT
/usr/bin/python3 -I "$source_dir/selfie-camera-control" configure "$station_file" > "$helper_file"
iface=$(sed -n "s/^INTERFACE = '\(.*\)'$/\1/p" "$helper_file")
printf '%s ALL=(root) NOPASSWD: /usr/local/libexec/selfie-camera-control reset, /usr/local/libexec/selfie-camera-control scan, /usr/local/libexec/selfie-camera-control sweep\n' "$app_user" > "$rule_file"
/usr/sbin/visudo -cf "$rule_file"
install -d -o root -g root -m 0755 /usr/local/libexec
install -o root -g root -m 0755 "$helper_file" /usr/local/libexec/selfie-camera-control
install -o root -g root -m 0440 "$rule_file" /etc/sudoers.d/selfie-camera
install -o root -g root -m 0644 "$source_dir/selfie-camera-adapter.service" /etc/systemd/system/selfie-camera-adapter.service
systemctl daemon-reload
systemctl enable selfie-camera-adapter.service
/usr/local/libexec/selfie-camera-control isolate
camera_profile=''
while IFS=: read -r uuid kind; do
    if [[ $kind == 802-11-wireless && $(nmcli -g connection.interface-name connection show uuid "$uuid") == "$iface" ]]; then
        camera_profile=$uuid
    fi
done < <(nmcli -t -f UUID,TYPE connection show)
if [[ -n $camera_profile ]]; then
    nmcli connection modify uuid "$camera_profile" ipv4.never-default yes ipv4.ignore-auto-dns yes ipv4.ignore-auto-routes yes
    printf 'The camera profile %s takes no default route, DNS server or DHCP route from the camera.\n' "$camera_profile"
else
    printf 'No NetworkManager profile is bound to %s; set ipv4.never-default, ipv4.ignore-auto-dns and ipv4.ignore-auto-routes to yes on the camera profile by hand.\n' "$iface" >&2
fi
printf 'Installed the camera helper and its sudo rule for %s, and enabled the boot check.\n' "$app_user"
printf 'Check the adapter now with: /usr/local/libexec/selfie-camera-control check\n'
