#!/usr/bin/env bash
# preflight_check.sh — run this the night before you leave.
# Every line should end ✅. Any ❌ or ⚠ tells you exactly what to fix.
# Safe to run as many times as you like; it changes nothing.

cd "$(dirname "$0")"
PASS="✅"; FAIL="❌"; WARN="⚠ "

echo "══════════ WINWEAVE TRAVEL PRE-FLIGHT ══════════"

# 1. The loop script itself
if [ -x daily_loop.sh ]; then echo "$PASS daily_loop.sh present and executable"
else echo "$FAIL daily_loop.sh missing or not executable  ->  cp ~/Downloads/daily_loop.sh . && chmod +x daily_loop.sh"; fi

# 2. Virtualenv + entry scripts compile
if [ -f .venv/bin/activate ]; then
    source .venv/bin/activate
    if python -m py_compile update_mlb_results.py auto_paper_tracker.py \
        scrapers/sgo_scraper.py 2>/dev/null; then
        echo "$PASS venv OK; all three loop scripts compile"
    else echo "$FAIL a loop script fails to compile — run each manually to see the error"; fi
else echo "$FAIL .venv not found at ./.venv"; fi

# 3. Secrets and database
[ -f keys.txt ] && grep -q "SGO_API_KEY=" keys.txt \
    && echo "$PASS keys.txt present with SGO_API_KEY" \
    || echo "$FAIL keys.txt missing or malformed"
[ -f data/winweave.db ] && echo "$PASS database present ($(du -h data/winweave.db | cut -f1))" \
    || echo "$FAIL data/winweave.db missing"

# 4. Cron daemon — THE Arch/XeroLinux gotcha: cronie is often not
#    even installed, and crontab entries do nothing without it.
if command -v crontab >/dev/null 2>&1; then
    if systemctl is-active --quiet cronie 2>/dev/null \
        || systemctl is-active --quiet crond 2>/dev/null \
        || systemctl is-active --quiet cron 2>/dev/null; then
        echo "$PASS cron daemon is running"
    else
        echo "$FAIL cron daemon NOT running  ->  sudo systemctl enable --now cronie"
    fi
else
    echo "$FAIL crontab command not found  ->  sudo pacman -S cronie && sudo systemctl enable --now cronie"
fi

# 5. The crontab entry exists
if crontab -l 2>/dev/null | grep -q "daily_loop.sh"; then
    echo "$PASS crontab entry found:"
    crontab -l | grep daily_loop.sh | sed 's/^/     /'
else
    echo "$FAIL no crontab entry  ->  crontab -e  and add:  0 11 * * * $PWD/daily_loop.sh"
fi

# 6. Suspend/sleep — a sleeping desktop runs nothing.
masked=$(systemctl is-enabled suspend.target 2>/dev/null)
if [ "$masked" = "masked" ]; then
    echo "$PASS system suspend is masked (machine cannot sleep)"
else
    echo "$WARN suspend not masked — EITHER set KDE: System Settings ->"
    echo "     Power Management -> Energy Saving -> uncheck 'Suspend session',"
    echo "     OR run: sudo systemctl mask sleep.target suspend.target hibernate.target"
    echo "     (undo later with: sudo systemctl unmask ...)"
fi

# 7. Internet + the two APIs the loop needs
if curl -s --max-time 8 "https://statsapi.mlb.com/api/v1/teams?sportId=1" >/dev/null; then
    echo "$PASS MLB Stats API reachable"
else echo "$FAIL cannot reach statsapi.mlb.com — check network"; fi
if curl -s --max-time 8 "https://api.sportsgameodds.com" >/dev/null 2>&1; then
    echo "$PASS SportsGameOdds host reachable"
else echo "$WARN SGO host check inconclusive (may still work — the scraper has retries)"; fi

# 8. Log file writable
touch data/cron_log.txt 2>/dev/null && echo "$PASS data/cron_log.txt writable" \
    || echo "$FAIL cannot write to data/cron_log.txt"

echo "═════════════════════════════════════════════════"
echo "All green? Do the 2-minute LIVE cron test in the guide,"
echo "then you're clear for departure."
