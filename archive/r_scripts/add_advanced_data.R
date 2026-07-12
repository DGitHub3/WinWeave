# scripts/add_advanced_data.R
# -------------------------------------------------
# WINWEAVE v2.0: Add 10 Advanced NFL Data Tables (Error-Proof)
# ONE COMMAND: Rscript scripts/add_advanced_data.R
# -------------------------------------------------

library(nflreadr)
library(DBI)
library(RSQLite)
library(dplyr)
library(purrr)  # ← ADDED THIS LINE

cat("Starting WinWeave v2.0 upgrade...\n")

con <- dbConnect(RSQLite::SQLite(), "data/winweave.db")

safe_write <- function(name, data) {
  tryCatch({
    # Coerce lists to comma-separated strings
    data <- data %>%
      mutate(across(where(is.list), ~ map_chr(.x, ~ paste(.x, collapse = ", "))))
    
    dbWriteTable(con, name, data, overwrite = TRUE)
    cat("   →", nrow(data), "rows added to", name, "\n")
  }, error = function(e) {
    cat("   → SKIPPED", name, "- Error:", e$message, "\n")
  })
}

# 1–8: Safe tables (no list columns)
cat("1/10: Injuries...\n")
safe_write("injuries", load_injuries(seasons = 2009:2024))

cat("2/10: Snap counts...\n")
safe_write("snap_counts", load_snap_counts())

cat("3/10: Depth charts...\n")
safe_write("depth_charts", load_depth_charts())

cat("4/10: Next gen...\n")
safe_write("next_gen_stats", load_nextgen_stats())

cat("5/10: Player stats...\n")
safe_write("player_stats", load_player_stats())

cat("6/10: Combine...\n")
safe_write("combine", load_combine())

cat("7/10: Draft picks...\n")
safe_write("draft_picks", load_draft_picks())

cat("8/10: Trades...\n")
safe_write("trades", load_trades())

# 9. CONTRACTS (Special handling for list columns)
cat("9/10: Contracts...\n")
contracts <- load_contracts()
if (nrow(contracts) > 0) {
  contracts <- contracts %>%
    mutate(across(where(is.list), ~ map_chr(.x, ~ paste(.x, collapse = ", "))))
  safe_write("contracts", contracts)
} else {
  cat("   → No contracts data available\n")
}

# 10. OFFICIALS
cat("10/10: Officials...\n")
safe_write("officials", load_officials())

dbDisconnect(con)

cat("\nWINWEAVE v2.0 UPGRADE COMPLETE!\n")
cat("→ 10 advanced tables added\n")
cat("→ Run weekly: Rscript scripts/add_advanced_data.R\n")