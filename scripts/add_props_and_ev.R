# scripts/add_props_and_ev.R
# -------------------------------------------------
# Add Player Props + Calculate EV
# -------------------------------------------------

library(nflreadr)
library(DBI)
library(RSQLite)
library(dplyr)
library(readr)

source("scripts/normalize_data.R")

# Connect to DB
con <- dbConnect(RSQLite::SQLite(), "data/winweave.db")

# --- 1. Download nflverse player props (historical stats) ---
cat("Downloading player props data...\n")
player_props_nflverse <- load_player_stats()

# --- 2. Normalize ---
player_props <- normalize(player_props_nflverse, "nflverse", "props") %>%
  mutate(source = "nflverse")

# --- 3. Save to DB ---
dbWriteTable(con, "props", player_props, overwrite = TRUE)

# --- 4. EV Calculator Function (Vectorized with ifelse) ---
calculate_ev <- function(true_prob, odds, stake = 100) {
  payout <- ifelse(odds > 0, odds, 100 / abs(odds) * 100)
  (true_prob * payout) - ((1 - true_prob) * stake)
}

# --- 5. Test EV on a Prop (e.g., Mahomes pass yds) ---
cat("\nTesting EV calculator on Mahomes pass yards...\n")
mahomes <- dbGetQuery(con, "SELECT * FROM props WHERE player_name LIKE '%Patrick Mahomes%' LIMIT 1")

if (nrow(mahomes) > 0) {
  avg_yards <- mahomes$passing_yards  # From historical average
  line <- 265.5  # Example prop line
  odds <- -110   # Example odds
  true_prob <- 1 - ppois(line, lambda = avg_yards)  # Poisson true prob
  ev <- calculate_ev(true_prob, odds)
  cat("Mahomes OVER", line, "yds @", odds, "→ True Prob:", round(true_prob*100, 1), "% → EV: +$", round(ev, 2), "\n")
} else {
  cat("No Mahomes data – add more props.\n")
}

# --- 6. Find +EV Bets (Example Loop) ---
cat("\nFinding +EV bets...\n")
props <- dbGetQuery(con, "SELECT * FROM props LIMIT 5")  # Expand to full query later

if (nrow(props) > 0 && "passing_yards" %in% names(props)) {
  ev_bets <- props %>%
    mutate(
      line = 265.5,  # Example line (replace with scraped odds)
      odds = -110,   # Example odds
      true_prob = 1 - ppois(line, lambda = passing_yards),
      ev = calculate_ev(true_prob, odds)
    ) %>%
    filter(ev > 5) %>%
    select(player_name, ev)
  
  write_csv(ev_bets, "data/ev_bets.csv")
  cat("Saved to data/ev_bets.csv\n")
  print(ev_bets)
} else {
  cat("No props data or 'passing_yards' column – check DB.\n")
}

dbDisconnect(con)
cat("\nWinWeave updated: Player props added + EV calculator ready.\n")