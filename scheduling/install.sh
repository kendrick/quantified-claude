#!/usr/bin/env bash
#
# Install the quantified-claude launchd agent (macOS). It renders the MOC once a
# day at 09:00, which also syncs this machine's events (render auto-collects).
#
# Usage:
#   scheduling/install.sh <data-dir>     # path to your private events repo
#   SKILL_MOC_DATA_DIR=... scheduling/install.sh
#
# Re-running reinstalls and reloads, so it's safe to run after editing the plist.

set -euo pipefail

DATA_DIR="${1:-${SKILL_MOC_DATA_DIR:-}}"
if [ -z "$DATA_DIR" ]; then
    echo "error: no data dir. Pass it as an argument or set SKILL_MOC_DATA_DIR." >&2
    echo "usage: scheduling/install.sh <data-dir>" >&2
    exit 1
fi

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
    "$TEMPLATE" > "$PLIST"

launchctl unload "$PLIST" 2>/dev/null || true
launchctl load "$PLIST"

echo "Installed $LABEL"
echo "  plist:    $PLIST"
echo "  command:  $PYTHON $SCRIPT render --data-dir $DATA_DIR"
echo "  schedule: daily at 09:00"
echo "  logs:     $LOGDIR/skill-usage.log"
echo
echo "Test it now without waiting for 09:00:"
echo "  launchctl start $LABEL && tail -f $LOGDIR/skill-usage.log"
