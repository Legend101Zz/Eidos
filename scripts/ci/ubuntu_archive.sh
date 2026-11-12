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
  # Some runners cannot use system DNS even as root. Bootstrap only the public Ubuntu repository
  # names through TLS-verified DNS over HTTPS; package signatures and the _apt identity stay intact.
  eidos_dns_json=$(mktemp)
  trap 'rm -f "$eidos_dns_json"' EXIT
  for eidos_dns_host in archive.ubuntu.com security.ubuntu.com; do
    if ! getent ahostsv4 "$eidos_dns_host" >/dev/null; then
      echo "Bootstrapping CI DNS for $eidos_dns_host"
      curl --fail --silent --show-error --connect-timeout 10 --max-time 20 \
        --resolve cloudflare-dns.com:443:1.1.1.1 -H 'accept: application/dns-json' \
        "https://cloudflare-dns.com/dns-query?name=$eidos_dns_host&type=A" -o "$eidos_dns_json"
      python3 - "$eidos_dns_host" "$eidos_dns_json" <<'PYDNS' >> /etc/hosts
import ipaddress
import json
import sys

with open(sys.argv[2]) as stream:
    response = json.load(stream)
if response.get("Status") != 0:
    raise SystemExit("Ubuntu DNS bootstrap failed")
addresses = [str(ipaddress.IPv4Address(item["data"])) for item in response.get("Answer", [])
             if item.get("type") == 1]
if not addresses:
    raise SystemExit("Ubuntu DNS bootstrap returned no IPv4 addresses")
print()
for address in addresses:
    print(address, sys.argv[1])
PYDNS
    fi
  done
  getent ahostsv4 archive.ubuntu.com >/dev/null
  if id _apt >/dev/null 2>&1; then runuser -u _apt -- getent ahostsv4 archive.ubuntu.com >/dev/null; fi
  apt-get -o Acquire::Retries=1 -o Acquire::http::Timeout=30 update -qq
  apt-get -o Acquire::Retries=1 -o Acquire::http::Timeout=30 install -y -qq "$@"
fi
