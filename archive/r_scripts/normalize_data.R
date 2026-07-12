# scripts/normalize_data.R
# -------------------------------------------------
# Normalize ANY NFL data source to WinWeave Standard
# -------------------------------------------------

library(yaml)
library(dplyr)

# Load mapping
mapping <- yaml.load_file("config/mapping.yaml")

normalize <- function(df, source, type) {
  map <- mapping[[source]][[type]]
  if (is.null(map)) return(df)
  
  # Only keep mappings that exist in df
  map <- map[names(map) %in% names(df)]
  
  if (length(map) == 0) return(df)
  
  # Rename using standard names
  df %>% rename(!!!map)
}