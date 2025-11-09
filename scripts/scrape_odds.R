#!/usr/bin/env Rscript
# scripts/scrape_odds.R
# -------------------------------------------------
# WinWeave – H2H Odds (WORKS – SAFE LOOP)
# -------------------------------------------------

library(httr)
library(jsonlite)
library(readr)

API_KEY <- "245cd492dd4ab69365e59bb7e91f531b"
SPORT   <- "americanfootball_nfl"
MARKETS <- "h2h"
REGIONS <- "us"
ODDS_FORMAT <- "american"

url <- paste0(
  "https://api.the-odds-api.com/v4/sports/", SPORT,
  "/odds/?apiKey=", API_KEY,
  "&regions=", REGIONS,
  "&markets=", MARKETS,
  "&oddsFormat=", ODDS_FORMAT
)

cat("Fetching H2H odds...\n")
resp <- GET(url)

if (status_code(resp) != 200) {
  cat("API ERROR:\n")
  cat(content(resp, "text"), "\n")
  stop("Request failed.")
}

raw <- content(resp, "text")
cat("Raw JSON (first 500 chars):\n")
cat(substr(raw, 1, 500), "\n\n")

games <- fromJSON(raw, flatten = FALSE)

# ---- Initialize empty data frame ----
all_odds <- data.frame(
  game_id = character(),
  home_team = character(),
  away_team = character(),
  commence_time = character(),
  book = character(),
  home_odds = character(),
  away_odds = character(),
  scrape_time = character(),
  stringsAsFactors = FALSE
)

cat("Found", length(games), "items. Parsing...\n")

# ---- Loop with full safety ----
for (i in seq_along(games)) {
  item <- games[[i]]
  
  # Skip if not a full game (no id or home_team)
  if (!is.list(item) || is.null(item[["id"]]) || is.null(item[["home_team"]])) next
  
  game <- item
  
  game_id <- game[["id"]]
  home_team <- game[["home_team"]]
  away_team <- game[["away_team"]]
  commence_time <- game[["commence_time"]]
  
  bookmakers <- game[["bookmakers"]]
  if (is.null(bookmakers) || length(bookmakers) == 0) next
  
  for (j in seq_along(bookmakers)) {
    bk <- bookmakers[[j]]
    
    markets <- bk[["markets"]]
    if (is.null(markets) || length(markets) == 0) next
    
    for (k in seq_along(markets)) {
      mkt <- markets[[k]]
      
      outcomes <- mkt[["outcomes"]]
      if (is.null(outcomes) || length(outcomes) < 2) next
      
      home_price <- NA_character_
      away_price <- NA_character_
      
      for (o in seq_along(outcomes)) {
        outcome <- outcomes[[o]]
        if (outcome[["name"]] == home_team) home_price <- as.character(outcome[["price"]])
        if (outcome[["name"]] == away_team) away_price <- as.character(outcome[["price"]])
      }
      
      if (is.na(home_price) || is.na(away_price)) next
      
      row <- data.frame(
        game_id = game_id,
        home_team = home_team,
        away_team = away_team,
        commence_time = commence_time,
        book = bk[["title"]] %||% NA_character_,
        home_odds = home_price,
        away_odds = away_price,
        scrape_time = as.character(Sys.time()),
        stringsAsFactors = FALSE
      )
      all_odds <- rbind(all_odds, row)
    }
  }
}

# ---- Save / print ----
output_file <- "data/live_odds.csv"

if (nrow(all_odds) == 0) {
  cat("No H2H odds found.\n")
} else {
  dir.create("data", showWarnings = FALSE, recursive = TRUE)
  write_csv(all_odds, output_file)
  cat("Saved", nrow(all_odds), "rows →", output_file, "\n")
  cat("First 3 rows:\n")
  print(head(all_odds, 3))
}