#!/usr/bin/env bash
#
# Install the quantified-claude launchd agent (macOS). It renders the MOC once a
# day at 10:05, which also syncs this machine's events (render auto-collects).
#
# Usage:
#   scheduling/install.sh <data-dir> [host]   # host defaults to $SKILL_MOC_HOST
#   SKILL_MOC_DATA_DIR=... scheduling/install.sh
#
# Re-running reinstalls and reloads, so it's safe to run after editing the plist.

set -euo pipefail

DATA_DIR="${1:-${SKILL_MOC_DATA_DIR:-}}"
if [ -z "$DATA_DIR" ]; then
    echo "error: no data dir. Pass it as an argument or set SKILL_MOC_DATA_DIR." >&2
    echo "usage: scheduling/install.sh <data-dir> [host]" >&2
    exit 1
fi

# Resolve the host key once, here, and bake it into the plist. launchd doesn't
# read your shell profile, and hostname() drifts with the network on machines
# that take a DHCP suffix — either would quietly start a second store file for
# this same laptop. Pass an explicit second argument on any machine whose
# hostname isn't already something stable.
HOST="${2:-${SKILL_MOC_HOST:-$(hostname)}}"

# Resolve everything to absolute paths — launchd won't expand $HOME or ~.
REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
SCRIPT="$REPO_DIR/skill_usage.py"
PYTHON="$(command -v python3)"
DATA_DIR="$(cd "$DATA_DIR" && pwd)"
LOGDIR="$HOME/Library/Logs/quantified-claude"
LABEL="com.quantified-claude.skill-usage"
TEMPLATE="$REPO_DIR/scheduling/$LABEL.plist"
PLIST="$HOME/Library/LaunchAgents/$LABEL.plist"

mkdir -p "$LOGDIR" "$HOME/Library/LaunchAgents"

# '|' as the sed delimiter so the '/' in paths doesn't need escaping.
sed -e "s|@PYTHON@|$PYTHON|g" \
    -e "s|@SCRIPT@|$SCRIPT|g" \
    -e "s|@DATADIR@|$DATA_DIR|g" \
    -e "s|@LOGDIR@|$LOGDIR|g" \
    -e "s|@HOST@|$HOST|g" \
    "$TEMPLATE" > "$PLIST"

launchctl unload "$PLIST" 2>/dev/null || true
launchctl load "$PLIST"

echo "Installed $LABEL"
echo "  plist:    $PLIST"
echo "  command:  $PYTHON $SCRIPT render --data-dir $DATA_DIR"
echo "  host:     $HOST"
echo "  schedule: daily at 10:05"
echo "  logs:     $LOGDIR/skill-usage.log"
echo
echo "Test it now without waiting for 10:05:"
echo "  launchctl start $LABEL && tail -f $LOGDIR/skill-usage.log"
