#!/usr/bin/env bash
# One-command installer / updater for MyDrive:
#   curl -fsSL https://raw.githubusercontent.com/psprashantsingla/mydrive/main/get.sh | sudo bash
set -euo pipefail
# Wrapped in a function so the whole script is read before anything runs (safe with curl | bash).
main() {
  REPO="${MYDRIVE_REPO:-psprashantsingla/mydrive}"
  REF="${MYDRIVE_REF:-main}"
  [[ $EUID -eq 0 ]] || { echo "Run with sudo:  curl -fsSL https://raw.githubusercontent.com/$REPO/$REF/get.sh | sudo bash" >&2; exit 1; }
  command -v curl >/dev/null && command -v tar >/dev/null || { echo "curl and tar are required" >&2; exit 1; }
  TMP="$(mktemp -d)"
  trap 'rm -rf "$TMP"' EXIT
  echo "==> Downloading MyDrive ($REPO@$REF)"
  curl -fsSL "https://github.com/$REPO/archive/refs/heads/$REF.tar.gz" | tar xz -C "$TMP" --strip-components=1
  bash "$TMP/install.sh"
}
main "$@"
