# Risk & Execution Plan (Archived Reference)

This consolidates `The_Plan` and `The_Plan_EXPORTED` (which were
duplicates of each other — one plain text, one HTML export from
Kate). Kept as reference for the original risk thinking, even
though the scraping implementation itself is being rebuilt in
Python.

---

## 1. Scraping: The Fragile Core

| Risk | Reality | Mitigation |
|---|---|---|
| Site changes | Sportsbook redesigns break scrapers | One script per book + weekly smoke test |
| Anti-bot | Cloudflare blocks IP after heavy request volume | Delays, randomized user-agent, proxy rotation later |
| Login walls | Some books require login for full props | Manual CSV input as fallback |
| Rate limits | Books ban after high request volume per hour | Run on a schedule, stagger books |

## 2. Account Management

| Constraint | Impact | Workaround |
|---|---|---|
| KYC / deposit limits | Capital spread thin across many books | Start with 3–5 books |
| Bonus hunting | Free bets come with rollover requirements | Use bonuses for +EV bets only |
| Withdrawal delays | 3–7 days typical | Keep a buffer per book |
| Geo-restrictions | Must be in a legal state | Use home IP, VPN only if ToS allows |

## 3. EV Scanner: Math vs. Reality

| Assumption | Risk | Fix |
|---|---|---|
| Poisson = truth | Overestimates low-sample players | Require a minimum game sample size |
| Lines are sharp | Books adjust fast | Run scans early, before public money moves lines |
| No juice modeling | -110 vs -115 matters | Add vig adjustment to EV calc |

## 4. Data Pipeline: The Silent Killer

| Failure | Symptom | Fix |
|---|---|---|
| DB corruption | Queries return 0 rows | Weekly backup of winweave.db |
| Table drift | player_id mismatches | Scheduled rebuild |
| CSV overwrite | Lose manual edits | Append + dedupe, never blind overwrite |

## 5. Time Budget

The original target was under 30 minutes a week for the manual
workflow: updating odds, running the EV scanner, placing bets.
Worth keeping as a sanity check — if the new Python pipeline adds
more manual overhead than this, something's over-engineered.
