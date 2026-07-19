#!/usr/bin/env bash
# daily_loop.sh — WinWeave unattended daily run (for cron).
#
# Suggested crontab (both lines; the afternoon run catches props that
# books post after lineups come out — dedupe makes re-runs safe):
#   0 11 * * * /home/kingdon/WinWeave/daily_loop.sh
#   0 16 * * * /home/kingdon/WinWeave/daily_loop.sh
#
# Order matters:
#   1. update_mlb_results — fetch last night's stats for pending
#      players + grade game bets from final scores + auto-resolve
#   2. sgo_scraper        — pull today's odds (props + game markets)
#   3. auto_paper_tracker — paper-track today's qualifying board
#      (--no-resolve: step 1 already resolved everything)
#
# Everything appends to data/cron_log.txt with timestamps.
# flock prevents two runs from overlapping if one runs long.

set -u
cd "$(dirname "$0")"

LOCK="data/.daily_loop.lock"
exec 9>"$LOCK"
if ! flock -n 9; then
    echo "$(date '+%F %H:%M') previous run still in progress — skipped" \
        >> data/cron_log.txt
    exit 0
fi

source .venv/bin/activate
LOG="data/cron_log.txt"
echo "" >> "$LOG"
echo "════════ daily_loop $(date '+%Y-%m-%d %H:%M') ════════" >> "$LOG"

echo "── 1/3 resolve yesterday ──" >> "$LOG"
python update_mlb_results.py               >> "$LOG" 2>&1

echo "── 2/3 pull today's odds ──" >> "$LOG"
python scrapers/sgo_scraper.py --league mlb >> "$LOG" 2>&1

echo "── 3/3 paper-track today ──" >> "$LOG"
python auto_paper_tracker.py --no-resolve   >> "$LOG" 2>&1

echo "════════ done $(date '+%H:%M') ════════" >> "$LOG"
