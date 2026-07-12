# scripts/scale_ev.R
# -------------------------------------------------
# SCALE EV SCANNER: +EV Player Props - FINAL
# -------------------------------------------------

library(DBI)
library(dplyr)
library(readr)

con <- dbConnect(RSQLite::SQLite(), "data/winweave.db")

# --- 1. Load live odds ---
cat("Loading live odds...\n")
live_odds <- read_csv("data/live_odds.csv", col_types = "cccdc") %>%
  mutate(odds = as.numeric(odds))

# --- 2. Pull QB stats ---
cat("Pulling QB stats...\n")
qb_stats <- dbGetQuery(con, "
  SELECT player_id, passing_yards
  FROM props
  WHERE position = 'QB' AND season >= 2020 AND passing_yards > 0
")

# --- 3. Average ---
qb_avg <- qb_stats %>%
  group_by(player_id) %>%
  summarise(avg_yards = mean(passing_yards), games = n(), .groups = "drop")

# --- 4. EV Calculator ---
calculate_ev <- function(true_prob, odds, stake = 100) {
  payout <- ifelse(odds > 0, odds, 100 / abs(odds) * 100)
  (true_prob * payout) - ((1 - true_prob) * stake)
}

# --- 5. Find +EV ---
ev_alerts <- live_odds %>%
  left_join(qb_avg, by = "player_id") %>%
  filter(!is.na(avg_yards), games >= 8) %>%
  mutate(
    true_prob = 1 - ppois(line, lambda = avg_yards),
    ev = calculate_ev(true_prob, odds)
  ) %>%
  filter(ev > 5) %>%
  select(player_display_name, prop, line, odds, avg_yards, games, true_prob, ev) %>%
  arrange(desc(ev))

# --- 6. Save ---
write_csv(ev_alerts, "data/ev_alerts.csv")
cat("Found", nrow(ev_alerts), "+EV bets! → data/ev_alerts.csv\n")
print(ev_alerts)

dbDisconnect(con)