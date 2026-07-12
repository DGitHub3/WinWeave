#!/usr/bin/env bash
# reorganize.sh — WinWeave project cleanup (2026-07-12)
#
# WHAT MOVES AND WHY:
#   archive/r_scripts/   <- single copy of the R era (scripts/ was a
#                           duplicate of archive/r_scripts/ — deleted)
#   archive/r_dashboard/ <- the old Shiny app (dashboard/app.R)
#   archive/logs/        <- test/verification terminal captures
#   archive/notes/       <- plans and cheat sheets from v1/v2
#   private/             <- personal & sensitive files (gitignored):
#                           bet ledger, balances, API KEYS
#
# WHAT DELIBERATELY DOES NOT MOVE:
#   All root .py entry points (dashboard, scanners, trackers, reports)
#   — they locate data/ relative to themselves and import each other
#   by module name. src/, scrapers/, data/, docs/, assets/, config/,
#   tests/ are already right. keys.txt stays at root: the scraper
#   reads it there.
#
# Safe to re-run: every move checks the file exists first.
# Uses git mv when the file is tracked (preserves history), plain mv
# otherwise.

set -e
cd "$(dirname "$0")"

mv_smart() {  # mv_smart <source> <destdir>
  [ -e "$1" ] || return 0
  mkdir -p "$2"
  if git ls-files --error-unmatch "$1" >/dev/null 2>&1; then
    git mv "$1" "$2/" && echo "  git mv $1 -> $2/"
  else
    mv "$1" "$2/" && echo "  mv     $1 -> $2/"
  fi
}

echo "── 1. Personal & sensitive -> private/ (gitignored) ──"
mkdir -p private
mv_smart "Book Balances 7-11-26"      private
mv_smart "backfill_bets_20260711.py"  private
mv_smart "API KEYS"                   private
mv_smart "data/sports_accounts.xlsx"  private

echo "── 2. R era -> archive/ (one copy, not two) ──"
# scripts/ duplicates archive/r_scripts/ file-for-file; keep archive's
if [ -d scripts ] && [ -d archive/r_scripts ]; then
  same=true
  for f in scripts/*.R; do
    base=$(basename "$f")
    cmp -s "$f" "archive/r_scripts/$base" || same=false
  done
  if $same; then
    git rm -r --cached scripts >/dev/null 2>&1 || true
    rm -rf scripts && echo "  removed scripts/ (byte-identical duplicate of archive/r_scripts/)"
  else
    echo "  ! scripts/ differs from archive/r_scripts/ — left in place, diff manually"
  fi
fi
mv_smart dashboard                archive/r_dashboard_tmp 2>/dev/null || true
[ -d archive/r_dashboard_tmp/dashboard ] && mv archive/r_dashboard_tmp/dashboard archive/r_dashboard && rmdir archive/r_dashboard_tmp 2>/dev/null || true
[ -d dashboard ] && { mkdir -p archive; mv dashboard archive/r_dashboard; echo "  mv dashboard/ -> archive/r_dashboard/"; }
mv_smart WinWeave_scripts.zip     archive/r_scripts
mv_smart HistoryTest1.Rhistory    archive/r_scripts
mv_smart .Rhistory                archive/r_scripts
mv_smart .RData                   archive/r_scripts

echo "── 3. Logs & test captures -> archive/logs/ ──"
mv_smart "First Scrape Test.txt"      archive/logs
mv_smart "Game-Level-Markets-Test"    archive/logs
mv_smart "Verification-Sequence.txt"  archive/logs
mv_smart "validate_data_test.txt"     archive/logs
mv_smart "data/nfl_audit_20260712.txt" archive/logs

echo "── 4. Old plans & cheat sheets -> archive/notes/ ──"
mv_smart "The Plan"                   archive/notes
mv_smart "The Plan EXPORTED"          archive/notes
mv_smart "Kate Cheat Sheet"           archive/notes
mv_smart "WINWEAVE_CHEAT_SHEET.txt"   archive/notes
mv_smart "WINWEAVE v2.0 CHEAT SHEET"  archive/notes

echo "── 5. Protect private/ in git ──"
grep -qx "private/" .gitignore 2>/dev/null || \
  { echo "private/" >> .gitignore; echo "  added private/ to .gitignore"; }

echo
echo "Done. Review with: git status   (private/ must NOT appear)"
echo "Then: git add -A && git commit -m 'chore: reorganize project structure' && git push"
