# scripts/predict.R
# -------------------------------------------------
# WINWEAVE PREDICTION ENGINE v1.4 — 5/5 GREEN WINS
# RUN: Rscript scripts/predict.R "Josh Allen" 285.5 -110 290 "NYJ"
# -------------------------------------------------

library(DBI)
library(dplyr)

args <- commandArgs(trailingOnly = TRUE)
if (length(args) < 5) {
  cat("
ERROR: Missing arguments
Usage: Rscript predict.R \"Player Name\" line odds opening_line opponent
Example: Rscript predict.R \"Josh Allen\" 285.5 -110 290 NYJ
")
  quit(status = 1)
}

player_name <- args[1]
line <- as.numeric(args[2])
odds <- as.numeric(args[3])
opening_line <- as.numeric(args[4])
opp <- args[5]

con <- tryCatch({
  dbConnect(RSQLite::SQLite(), "data/winweave.db")
}, error = function(e) {
  cat("ERROR: Cannot connect to database\n")
  quit(status = 1)
})

predict_edge <- function(player_name, line, odds, opening_line, opp) {
  player_id <- tryCatch({
    dbGetQuery(con, paste0("
      SELECT player_id FROM props WHERE player_display_name = '", player_name, "' LIMIT 1
    "))$player_id
  }, error = function(e) NULL)
  if (length(player_id) == 0 || is.na(player_id)) {
    cat("ERROR: Player '", player_name, "' not found.\n")
    return()
  }
  
  # 1. WORKLOAD
  workload <- tryCatch({
    dbGetQuery(con, paste0("
      SELECT AVG(offense_pct) FROM snap_counts WHERE player = '", player_name, "' AND week >= 7
    "))[[1]]
  }, error = function(e) NA)
  workload_status <- if(is.na(workload) || workload > 0.65) "GREEN" else "RED"
  
  # 2. MATCHUP
  matchup <- tryCatch({
    dbGetQuery(con, paste0("
      SELECT AVG(passing_yards) FROM props WHERE opponent_team = '", opp, "' AND season = 2025
    "))[[1]]
  }, error = function(e) NA)
  matchup_rank <- if(is.na(matchup)) 99 else {
    tryCatch({
      dbGetQuery(con, paste0("
        SELECT COUNT(*) FROM (
          SELECT opponent_team, AVG(passing_yards) AS yds 
          FROM props WHERE season = 2025 GROUP BY opponent_team
        ) WHERE yds > ", matchup, "
      "))[[1]] + 1
    }, error = function(e) 99)
  }
  matchup_status <- if(matchup_rank > 22) "GREEN" else "RED"
  
  # 3. EPA
  epa <- tryCatch({
    dbGetQuery(con, paste0("
      SELECT AVG(passing_epa) FROM props WHERE player_id = '", player_id, "' AND week >= 7
    "))[[1]]
  }, error = function(e) NA)
  epa_status <- if(is.na(epa) || epa > 0.3) "GREEN" else "RED"
  
  # 4. INJURY
  injury <- tryCatch({
    dbGetQuery(con, paste0("
      SELECT practice_status FROM injuries WHERE full_name = '", player_name, "' ORDER BY week DESC LIMIT 1
    "))[[1]]
  }, error = function(e) NA)
  injury_status <- if(is.na(injury) || injury == "Full Participation in Practice") "GREEN" else "RED"
  
  # 5. LINE MOVEMENT
  line_drop <- opening_line - line
  line_status <- if(line_drop >= 3) "GREEN" else "RED"
  
  # 6. EPA-ADJUSTED EV
  base_yards <- tryCatch({
    dbGetQuery(con, paste0("
      SELECT AVG(passing_yards) FROM props WHERE player_id = '", player_id, "' AND season = 2025
    "))[[1]]
  }, error = function(e) 0)
  epa_boost <- ifelse(is.na(epa), 0, epa * 10)
  adjusted_yards <- base_yards + epa_boost
  true_prob <- 1 - ppois(line, lambda = max(1, adjusted_yards))
  payout <- ifelse(odds > 0, odds, 100 / abs(odds) * 100)
  ev <- (true_prob * payout) - ((1 - true_prob) * 100)
  
  green_count <- sum(
    workload_status == "GREEN",
    matchup_status == "GREEN",
    epa_status == "GREEN",
    injury_status == "GREEN",
    line_status == "GREEN"
  )
  
  cat("
5/5 GREEN LIGHT TEST
Workload: ", round(workload*100,1), "% → ", workload_status, "
", opp, " Rank: ", matchup_rank, " → ", matchup_status, "
EPA: ", round(epa,2), " → ", epa_status, "
Injury: ", ifelse(is.na(injury), "No Report", injury), " → ", injury_status, "
Line Drop: ", line_drop, " pts → ", line_status, "

EPA-ADJUSTED EV
Base Yards: ", round(base_yards,1), "
EPA Boost: +", round(epa_boost,1), "
Adjusted Yards: ", round(adjusted_yards,1), "
TRUE PROB: ", round(true_prob*100,1), "% → +EV: $", round(ev,2), "
", if(green_count >= 5) "BET NOW" else "PASS", "\n")
}

predict_edge(player_name, line, odds, opening_line, opp)
dbDisconnect(con)