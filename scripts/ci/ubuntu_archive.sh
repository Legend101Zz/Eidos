#!/usr/bin/env bash
# GitHub's Ubuntu image can point apt at azure.archive.ubuntu.com through mirror+file sources.
# That mirror repeatedly failed DNS during verification. Use the official Ubuntu archive instead;
# retain the configured suites, components and apt's normal signed-metadata/package verification.
set -euo pipefail
[ "$(id -u)" = 0 ] || { echo "run as root to configure CI apt sources" >&2; exit 2; }
for eidos_apt_source in /etc/apt/apt-mirrors*.txt /etc/apt/sources.list /etc/apt/sources.list.d/*.list /etc/apt/sources.list.d/*.sources; do
  [ -f "$eidos_apt_source" ] || continue
  if grep -q 'azure[.]archive[.]ubuntu[.]com' "$eidos_apt_source"; then
    sed -i 's/azure\.archive\.ubuntu\.com/archive.ubuntu.com/g' "$eidos_apt_source"
  fi
done
