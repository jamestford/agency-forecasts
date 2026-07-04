#!/bin/bash
# Wrapper script for the weekly DOC forecast run on this Mac.
# Invoked by launchd; see ~/Library/LaunchAgents/com.jamestford.agency-forecasts-doc.plist.
#
# Cloudflare hardened Turnstile detection to the point where Camoufox on
# GitHub Actions can't clear it anymore. This Mac's residential IP does.
set -euo pipefail

REPO="$HOME/dev/agency-forecasts"
LOG_DIR="$HOME/Library/Logs/agency-forecasts"
mkdir -p "$LOG_DIR"
LOG="$LOG_DIR/doc-$(date -u +%Y%m%dT%H%M%SZ).log"

# Cap log directory to last 30 files
find "$LOG_DIR" -name "doc-*.log" | sort -r | tail -n +31 | xargs rm -f 2>/dev/null || true

echo "[$(date -u '+%Y-%m-%dT%H:%M:%SZ')] Starting DOC forecast run" | tee -a "$LOG"

cd "$REPO"
# Sync with remote first (other workflows may have committed since last run)
git fetch origin main >>"$LOG" 2>&1
git reset --hard origin/main >>"$LOG" 2>&1

# Run the fetcher
if ! .venv/bin/python scripts/fetch_doc_forecast.py >>"$LOG" 2>&1; then
    echo "[$(date -u '+%Y-%m-%dT%H:%M:%SZ')] fetch failed; see log" | tee -a "$LOG"
    exit 1
fi

# Commit and push if there are changes under doc/
if [[ -n "$(git status --porcelain current/doc archive/doc)" ]]; then
    git config user.email "james.t.ford@gmail.com"
    git config user.name "James Ford"
    git add current/doc archive/doc
    # Extract diff summary from the just-updated metadata for the commit body
    DIFF=$(python3 -c "
import json
try:
    m = json.load(open('current/doc/metadata.json'))
    print(m['files'][0].get('diff_summary', '') or '')
except Exception:
    pass
" 2>/dev/null)
    TODAY=$(date -u +%Y-%m-%d)
    {
        echo "DOC procurement forecast updated — $TODAY"
        echo
        echo "Ran locally on Mac via launchd (Cloudflare blocks GH Actions)."
        echo "Source: https://www.commerce.gov/oam/industry/procurement-forecasts"
        [[ -n "$DIFF" ]] && echo "Diff:   $DIFF"
        echo
        [[ -n "$DIFF" ]] && echo "See archive/doc/$TODAY/changes.md for row-level changes."
    } > /tmp/doc-commit-msg.txt
    git commit -F /tmp/doc-commit-msg.txt >>"$LOG" 2>&1
    git push >>"$LOG" 2>&1
    echo "[$(date -u '+%Y-%m-%dT%H:%M:%SZ')] Pushed update: $DIFF" | tee -a "$LOG"
else
    echo "[$(date -u '+%Y-%m-%dT%H:%M:%SZ')] No changes to commit." | tee -a "$LOG"
fi
