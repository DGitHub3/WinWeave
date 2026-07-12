# scripts/download_and_build.R
# -------------------------------------------------
# Download + Normalize + Save to DB (PBP Added)
# -------------------------------------------------

library(nflreadr)
library(DBI)
library(RSQLite)
library(dplyr)

source("scripts/normalize_data.R")

con <- dbConnect(RSQLite::SQLite(), "data/winweave.db")

# Rosters (1985–2025)
cat("Downloading rosters 1985–2025...\n")
rosters_nflverse <- bind_rows(lapply(1985:2025, function(y) {
  cat("Year:", y, "\n")
  df <- load_rosters(season = y)
  cols_to_char <- c("jersey_number", "draft_number", "height", "weight", "birth_date", "college", "status", "depth_chart_position")
  for (col in cols_to_char) {
    if (col %in% names(df)) df[[col]] <- as.character(df[[col]])
  }
  df
}))

# Games (1920–2025)
cat("Downloading games...\n")
games_nflverse <- load_schedules()

# PBP (1999–2025)
cat("Downloading PBP 1999–2025...\n")
pbp_nflverse <- bind_rows(lapply(1999:2025, function(y) {
  cat("Year:", y, "\n")
  load_pbp(seasons = y)
}))

# Normalize & Save
rosters <- normalize(rosters_nflverse, "nflverse", "roster") %>% mutate(source = "nflverse")
games <- normalize(games_nflverse, "nflverse", "game") %>% mutate(source = "nflverse")

dbWriteTable(con, "rosters", rosters, overwrite = TRUE)
dbWriteTable(con, "games", games, overwrite = TRUE)
dbWriteTable(con, "pbp", pbp_nflverse, overwrite = TRUE)

# Test: Jalen Hurts PBP (2025 Week 1)
cat("\nTesting PBP: Jalen Hurts 2025 Week 1\n")
hurts_pbp <- dbGetQuery(con, "
  SELECT play_id, play_type, yards_gained, epa, passer_player_id
  FROM pbp
  WHERE passer_player_id = (SELECT player_id FROM props WHERE player_display_name = 'Jalen Hurts' LIMIT 1)
    AND season = 2025 AND week = 1
  LIMIT 5
")
print(hurts_pbp)

dbDisconnect(con)
cat("\nWinWeave DB ready: PBP added (1999–2025)!\n")