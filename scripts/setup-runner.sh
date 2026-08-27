#!/usr/bin/env bash
# One-time: register THIS machine as the GitHub Actions runner that deploys.
#
#   scripts/setup-runner.sh [--version 2.328.0]
#
# Everything here is user-level. The single thing it cannot do without root is
# make the runner survive a logout/reboot; it prints that one command at the end.
set -euo pipefail

REPO="${RUNNER_REPO:-namanjain221995/personal-LLM-Chabot}"
VERSION="${RUNNER_VERSION:-2.328.0}"
DIR="${RUNNER_DIR:-$HOME/actions-runner}"
LABELS="${RUNNER_LABELS:-self-hosted,dgx-spark,arm64}"
NAME="${RUNNER_NAME:-$(hostname)}"
[ "${1:-}" = "--version" ] && VERSION="$2"

command -v gh >/dev/null || { echo "gh CLI is required (it mints the registration token)" >&2; exit 2; }
gh auth status >/dev/null 2>&1 || { echo "run: gh auth login" >&2; exit 2; }
case "$(uname -m)" in aarch64|arm64) ARCH=arm64 ;; x86_64) ARCH=x64 ;; *) echo "unsupported arch" >&2; exit 2 ;; esac

mkdir -p "$DIR"; cd "$DIR"
if [ ! -x ./config.sh ]; then
  TARBALL="actions-runner-linux-${ARCH}-${VERSION}.tar.gz"
  echo "==> downloading $TARBALL"
  curl -fsSLO "https://github.com/actions/runner/releases/download/v${VERSION}/${TARBALL}"
  tar xzf "$TARBALL" && rm -f "$TARBALL"
fi

if [ -f .runner ]; then
  echo "==> already configured as '$(python3 -c 'import json;print(json.load(open(".runner"))["agentName"])' 2>/dev/null || echo unknown)'"
else
  echo "==> requesting a registration token for $REPO"
  TOKEN="$(gh api -X POST "repos/${REPO}/actions/runners/registration-token" --jq .token)"
  ./config.sh --unattended --replace \
    --url "https://github.com/${REPO}" --token "$TOKEN" \
    --name "$NAME" --labels "$LABELS" --work _work
fi

# A user unit needs no root; lingering (surviving logout) does.
UNIT="$HOME/.config/systemd/user/github-runner.service"
mkdir -p "$(dirname "$UNIT")"
cat > "$UNIT" <<UNITEOF
[Unit]
Description=GitHub Actions runner (${REPO})
After=network-online.target docker.service

[Service]
ExecStart=${DIR}/run.sh
WorkingDirectory=${DIR}
Restart=always
RestartSec=5
# The deploy needs the user's Docker access and the same PATH as an interactive
# shell (the techsara shim bootstraps uv under \$HOME).
Environment=HOME=${HOME}
Environment=PATH=${HOME}/.local/bin:/usr/local/bin:/usr/bin:/bin

[Install]
WantedBy=default.target
UNITEOF
systemctl --user daemon-reload
systemctl --user enable --now github-runner.service
sleep 3
systemctl --user --no-pager --lines=5 status github-runner.service || true

echo
echo "==> runner registered with labels: ${LABELS}"
if [ "$(loginctl show-user "$USER" --property=Linger --value 2>/dev/null)" != "yes" ]; then
  cat <<NOTE

  ONE command still needs root, so the runner survives logout and reboot:

      sudo loginctl enable-linger $USER

  Without it the runner stops when your session ends and merges will not deploy.
NOTE
fi
