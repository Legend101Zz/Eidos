#!/usr/bin/env bash
# GitHub's Ubuntu image can point apt at azure.archive.ubuntu.com through mirror+file sources.
# That mirror repeatedly failed DNS during verification. Use the official Ubuntu archive instead;
# retain the configured suites, components and apt's normal signed-metadata/package verification.
# Optional arguments are the packages to install. sudo callers preserve proxy variables explicitly.
set -euo pipefail
[ "$(id -u)" = 0 ] || { echo "run as root to configure CI apt sources" >&2; exit 2; }
for eidos_apt_source in /etc/apt/apt-mirrors*.txt /etc/apt/sources.list /etc/apt/sources.list.d/*.list /etc/apt/sources.list.d/*.sources; do
  [ -f "$eidos_apt_source" ] || continue
  if grep -q 'azure[.]archive[.]ubuntu[.]com' "$eidos_apt_source"; then
    sed -i 's/azure\.archive\.ubuntu\.com/archive.ubuntu.com/g' "$eidos_apt_source"
  fi
done

# apt uses lowercase proxy variables; hosted runners may provide only uppercase variants.
# Never print proxy values: they may contain credentials.
if [ -z "${http_proxy:-}" ] && [ -n "${HTTP_PROXY:-}" ]; then export http_proxy="$HTTP_PROXY"; fi
if [ -z "${https_proxy:-}" ] && [ -n "${HTTPS_PROXY:-}" ]; then export https_proxy="$HTTPS_PROXY"; fi
if [ -z "${no_proxy:-}" ] && [ -n "${NO_PROXY:-}" ]; then export no_proxy="$NO_PROXY"; fi
if [ "$#" -gt 0 ]; then
  # Diagnose DNS under both identities without exposing proxy values or runner credentials.
  echo "CI apt DNS preflight (root): archive.ubuntu.com"
  getent ahostsv4 archive.ubuntu.com >/dev/null || { echo "root cannot resolve the Ubuntu archive" >&2; exit 1; }
  if id _apt >/dev/null 2>&1; then
    echo "CI apt DNS preflight (_apt): archive.ubuntu.com"
    runuser -u _apt -- getent ahostsv4 archive.ubuntu.com >/dev/null || {
      echo "apt's download identity cannot resolve the Ubuntu archive" >&2; exit 1;
    }
  fi
  apt-get -o Acquire::Retries=1 -o Acquire::http::Timeout=30 update -qq
  apt-get -o Acquire::Retries=1 -o Acquire::http::Timeout=30 install -y -qq "$@"
fi
