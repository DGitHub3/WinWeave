# WinWeave — Owner's Manual
*Written 2026-07-12. Keep this in docs/. Read it whenever you forget how your own machine works.*

---

## What you built (one paragraph)
WinWeave finds betting edges by comparing its own predictions to the
sportsbooks' prices — but its real superpower is **honesty**: every
prediction is saved, graded after the game, and scored. Right now it
is in **proving mode**: you track picks with $0 until the report card
says the model is real.

---

## Your daily routine (5 minutes)
1. Open the dashboard: `streamlit run dashboard.py`
2. Click **Pull Fresh Odds (MLB)**
3. On Top Picks, click **📋 Paper-track all untracked** (one click = your old top-10 ritual)
4. Click **🔄 Fetch results & auto-resolve** (grades yesterday's picks automatically)
5. Glance at the **calibration progress bar** — that's the score that matters

That's it. No money. The bar filling up IS the work.

## Once a week
```
python scrapers/build_mlb_db.py        # full MLB data refresh (~20 min)
python calibration_report.py --paper-only --since 2026-07-12
```

---

## What the grades mean (plain words)
- **X — Audit first** = "This looks too good. Something is probably
  broken. DO NOT BET. Never bet an X. Ever."
- **A** = strong pick, rare on purpose
- **B** = decent pick — this is what good looks like
- **C** = barely a pick
- **F** = no edge, skip (most props — this is normal, not broken)

**If the board is ever wall-to-wall A's at huge EV%, the model is
broken, not brilliant.** Boring is healthy.

---

## When can I bet real money again?
When ALL THREE are true in `python calibration_report.py --paper-only --since 2026-07-12`:
1. **150+ graded results** (the progress bar says "ready!")
2. **Model beats the market** on Brier score (the report literally
   prints "Model beats the raw market baseline")
3. You bet **only** the stat types the ROI table shows are working
   (early evidence points at **strikeouts**)

Then: quarter-Kelly stakes, 2% of bankroll max per bet. If the report
says the model LOSES to the market — WinWeave's job becomes telling
you not to bet, and that still saves you money.

## Rules that saved you money already
1. Never bet an **X** grade.
2. Never bet **against** the model's own number (your <50% bucket hit 12%).
3. Never chase one player across books (remember PCA, 1-5).
4. **Bonus bets and boosts are your only guaranteed edge** — spend
   them on high-probability spots, not longshots.
5. Feeling a conviction itch? It's already paper-tracked. Let the
   record answer.

---

## NFL countdown (season starts Sept 9)
- **Early Aug:** preseason odds appear → run `python scrapers/sgo_scraper.py --league nfl` once to verify the pipeline. Paper-track only. Preseason games are noise.
- **Sept 1 week:** `python refresh_season_data.py` (then weekly, every Tue/Wed all season — this is also how NFL bets auto-resolve)
- **Weeks 1–3:** paper only, no exceptions. Books are sharpest in September.
- Quick cleanup someday: `sqlite3 data/winweave.db "UPDATE injuries SET practice_status = TRIM(practice_status)"`

---

## When something breaks (it's usually one of these)
| Symptom | Fix |
|---|---|
| "cannot import name..." after an install | A stale download. Check for `file(1).py` in Downloads — copy THAT one. Then clear caches: `find . -name "__pycache__" -exec rm -rf {} +` and fully restart Streamlit (Ctrl-C, not refresh). |
| Bets won't resolve | Click **Fetch results & auto-resolve** — it now tells you WHY each one is stuck. "Not final yet" = wait. "Ambiguous" = resolve manually: `python track_result.py --id N --value X` |
| `cp: cannot stat` | The file was never downloaded. Click the file card in the chat first. |
| Weird terminal errors on `ls -t` | Your system aliases ls to eza. Use full paths instead of clever commands. |

## Before every `git push` (30 seconds, every time)
```
git status
```
If you see **keys.txt, API KEYS, anything "Book Balances", anything
in private/ or data/** → STOP. Those must never reach GitHub.

## Where things live
- `*.py` at root = the commands you type (they stay at root — moving them breaks paths)
- `src/` = the brain (calibration.py is the most important file in the project)
- `private/` = your money stuff (can never be pushed, by design)
- `archive/` = history · `data/winweave.db` = everything the model knows

*You built a machine that tells you the truth. Trust the report card
more than any single pick — including the ones that feel certain.*
