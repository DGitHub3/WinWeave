# scripts/download_and_build.R
# -------------------------------------------------
# Download + Normalize + Save to DB
# -------------------------------------------------

library(nflreadr)
library(readr)
library(DBI)
library(RSQLite)
library(dplyr)

source("scripts/normalize_data.R")

# Connect to DB
con <- dbConnect(RSQLite::SQLite(), "data/winweave.db")

# --- 1. Download nflverse data ---
cat("Downloading nflverse data...\n")
rosters_nflverse <- load_rosters()
games_nflverse <- load_schedules()

# --- 2. Normalize ---
rosters <- normalize(rosters_nflverse, "nflverse", "roster") %>%
  mutate(source = "nflverse")
games <- normalize(games_nflverse, "nflverse", "game") %>%
  mutate(source = "nflverse")

# --- 3. Save to DB ---
dbWriteTable(con, "rosters", rosters, overwrite = TRUE)
dbWriteTable(con, "games", games, overwrite = TRUE)

# --- 4. Test Query: "Bears" ---
cat("\nTesting query: Bears roster + games\n")
bears <- dbGetQuery(con, "
  SELECT r.*, g.*
  FROM rosters r
  LEFT JOIN games g ON r.team = g.home_team AND r.season = g.season
  WHERE r.team = 'CHI'
  LIMIT 5
")
print(bears)

dbDisconnect(con)
cat("\nWinWeave DB ready: data/winweave.db\n")