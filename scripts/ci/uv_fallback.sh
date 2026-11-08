#!/usr/bin/env bash
# Runs only when astral-sh/setup-uv failed (its step has continue-on-error so this can run).
# The PyPI fallback exists for one failure: the GitHub release download of uv being unavailable.
# Any other setup-uv failure is NOT masked: if the release download works, the job fails here.
set -euo pipefail
UV_FALLBACK_VERSION="${UV_FALLBACK_VERSION:-0.13.7}"
case "$(uname -s)-$(uname -m)" in
  Linux-x86_64) asset="uv-x86_64-unknown-linux-gnu.tar.gz" ;;
  Darwin-arm64) asset="uv-aarch64-apple-darwin.tar.gz" ;;
  *) echo "::error::uv fallback: unsupported runner $(uname -s)-$(uname -m)"; exit 1 ;;
esac
probe="https://github.com/astral-sh/uv/releases/download/${UV_FALLBACK_VERSION}/${asset}"
if curl -fsSL --retry 3 --retry-delay 5 --max-time 120 -o /dev/null "$probe"; then
  echo "::error::setup-uv failed, but the GitHub release download works ($probe); this is not the CDN failure the fallback covers, so the job fails instead of masking it"
  exit 1
fi
echo "::warning::GitHub release download of uv is unavailable; installing uv==${UV_FALLBACK_VERSION} from PyPI"
python3 -m pip install --user --break-system-packages "uv==${UV_FALLBACK_VERSION}" \
  || python3 -m pip install --user "uv==${UV_FALLBACK_VERSION}"
bindir="$(python3 -m site --user-base)/bin"
"$bindir/uv" --version
echo "$bindir" >> "$GITHUB_PATH"
echo "uv: setup-uv failed (GitHub release download unavailable); used the pinned PyPI fallback uv==${UV_FALLBACK_VERSION}" >> "$GITHUB_STEP_SUMMARY"
