# scripts/query_helpers.R
# -------------------------------------------------
# Helper functions to pull player & team stats
# -------------------------------------------------

library(DBI)
library(dplyr)

# Open connection ONCE when script loads
con <- dbConnect(RSQLite::SQLite(), "data/winweave.db")

# PLAYER: Get full career
get_player_career <- function(full_name) {
  dbGetQuery(con, "
    SELECT season, team, position, jersey_number, status
    FROM rosters
    WHERE full_name LIKE ?
    ORDER BY season
  ", params = list(paste0("%", full_name, "%")))
}

# PLAYER: Get one season
get_player_season <- function(full_name, season) {
  dbGetQuery(con, "
    SELECT *
    FROM rosters
    WHERE full_name LIKE ? AND season = ?
  ", params = list(paste0("%", full_name, "%"), season))
}

# TEAM: Get roster
get_team_roster <- function(team, season) {
  dbGetQuery(con, "
    SELECT full_name, position, jersey_number, status
    FROM rosters
    WHERE team = ? AND season = ?
    ORDER BY position, full_name
  ", params = list(team, season))
}

# TEAM: Get games
get_team_games <- function(team, season) {
  dbGetQuery(con, "
    SELECT game_id, gameday, 
           CASE WHEN home_team = ? THEN away_team ELSE home_team END AS opponent,
           CASE WHEN home_team = ? THEN home_score ELSE away_score END AS pf,
           CASE WHEN home_team = ? THEN away_score ELSE home_score END AS pa,
           result
    FROM games
    WHERE (home_team = ? OR away_team = ?) AND season = ?
    ORDER BY gameday
  ", params = list(team, team, team, team, team, season))
}

# OPTIONAL: Close when R session ends
# on.exit(dbDisconnect(con), add = TRUE)