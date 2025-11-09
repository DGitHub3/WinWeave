# scripts/download_and_build.R
# -------------------------------------------------
# Download + Normalize + Save to DB (1985–2025) - FINAL
# -------------------------------------------------

library(nflreadr)
library(DBI)
library(RSQLite)
library(dplyr)

source("scripts/normalize_data.R")

con <- dbConnect(RSQLite::SQLite(), "data/winweave.db")

cat("Downloading rosters 1985–2025...\n")
rosters_nflverse <- bind_rows(lapply(1985:2025, function(y) {
  cat("Year:", y, "\n")
  df <- load_rosters(season = y)
  
  # FIX: Force problematic columns to character
  cols_to_char <- c("jersey_number", "draft_number", "height", "weight", 
                    "birth_date", "college", "status", "depth_chart_position")
  for (col in cols_to_char) {
    if (col %in% names(df)) {
      df[[col]] <- as.character(df[[col]])
    }
  }
  df
}))

cat("Downloading games 1920–2025...\n")
games_nflverse <- load_schedules()

# Normalize
rosters <- normalize(rosters_nflverse, "nflverse", "roster") %>% mutate(source = "nflverse")
games <- normalize(games_nflverse, "nflverse", "game") %>% mutate(source = "nflverse")

# Save
dbWriteTable(con, "rosters", rosters, overwrite = TRUE)
dbWriteTable(con, "games", games, overwrite = TRUE)

# Test: Michael Vick 2002
cat("\nTesting: Michael Vick in 2002\n")
vick <- dbGetQuery(con, "
  SELECT full_name, position, team, season
  FROM rosters
  WHERE full_name LIKE '%Michael Vick%' AND season = 2002
")
print(vick)

dbDisconnect(con)
cat("\nWinWeave DB ready: 1985–2025 rosters + full games!\n")