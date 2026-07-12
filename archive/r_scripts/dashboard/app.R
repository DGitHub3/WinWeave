# dashboard/app.R
# -------------------------------------------------
# WINWEAVE PREDICTION ENGINE v1.0
# ONE BUTTON: PREDICT +EV
# -------------------------------------------------

library(shiny)
library(DBI)
library(dplyr)
library(readr)
library(DT)
library(readxl)

# CONNECT TO DB
con <- dbConnect(RSQLite::SQLite(), "../data/winweave.db")

ui <- fluidPage(
  titlePanel("WINWEAVE PREDICTION ENGINE v1.0"),
  tags$head(
    tags$style(HTML("
      .green { color: green; font-weight: bold; }
      .red { color: red; font-weight: bold; }
      .btn-predict { background-color: #2ca02c; color: white; width: 100%; }
    "))
  ),
  
  sidebarLayout(
    sidebarPanel(
      width = 4,
      selectInput("player", "Player:", 
                  choices = dbGetQuery(con, "SELECT DISTINCT player_display_name FROM props ORDER BY player_display_name")$player_display_name),
      selectInput("prop", "Prop:", choices = c("pass_yards", "rush_yards", "total_yards")),
      numericInput("line", "Line:", 235.5),
      numericInput("odds", "Odds:", -110),
      actionButton("predict", "PREDICT +EV", class = "btn-predict"),
      hr(),
      numericInput("bet_amount", "Bet Amount ($):", 100),
      actionButton("log", "LOG BET", class = "btn-success")
    ),
    
    mainPanel(
      width = 8,
      h3("4/5 GREEN LIGHT TEST"),
      verbatimTextOutput("pillars"),
      hr(),
      h3("PREDICTION"),
      verbatimTextOutput("result"),
      hr(),
      h4("Bankroll"),
      DTOutput("bankroll")
    )
  )
)

server <- function(input, output) {
  
  player_id <- reactive({
    dbGetQuery(con, paste0("SELECT player_id FROM props WHERE player_display_name = '", input$player, "' LIMIT 1"))$player_id
  })
  
  # 4/5 PILLARS
  pillars <- reactive({
    req(player_id())
    
    # 1. WORKLOAD
    workload <- dbGetQuery(con, paste0("
      SELECT AVG(offense_pct) FROM snap_counts 
      WHERE player = '", input$player, "' AND week >= 7
    "))[[1]]
    workload_status <- if(workload > 0.65) "GREEN" else "RED"
    
    # 2. MATCHUP (DAL = opponent)
    opp <- "DAL"
    matchup <- dbGetQuery(con, paste0("
      SELECT AVG(passing_yards) FROM props 
      WHERE opponent_team = '", opp, "' AND season = 2025
    "))[[1]]
    matchup_rank <- dbGetQuery(con, "
      SELECT COUNT(*) FROM (
        SELECT opponent_team, AVG(passing_yards) AS yds 
        FROM props WHERE season = 2025 GROUP BY opponent_team
      ) WHERE yds > ", matchup, "
    ") + 1
    matchup_status <- if(matchup_rank > 22) "GREEN" else "RED"
    
    # 3. EPA
    epa <- dbGetQuery(con, paste0("
      SELECT AVG(passing_epa) FROM props 
      WHERE player_id = '", player_id(), "' AND week >= 7
    "))[[1]]
    epa_status <- if(epa > 0.3) "GREEN" else "RED"
    
    # 4. INJURY
    injury <- dbGetQuery(con, paste0("
      SELECT practice_status FROM injuries 
      WHERE full_name = '", input$player, "' 
      ORDER BY week DESC LIMIT 1
    "))[[1]]
    injury_status <- if(is.na(injury) || injury == "Full Participation in Practice") "GREEN" else "RED"
    
    list(
      workload = paste("Workload:", round(workload*100,1), "% →", workload_status),
      matchup = paste("DAL Rank:", matchup_rank, "→", matchup_status),
      epa = paste("EPA:", round(epa,2), "→", epa_status),
      injury = paste("Injury:", ifelse(is.na(injury), "No Report", injury), "→", injury_status)
    )
  })
  
  # +EV CALC
  observeEvent(input$predict, {
    req(player_id())
    avg <- dbGetQuery(con, paste0("
      SELECT AVG(passing_yards) FROM props 
      WHERE player_id = '", player_id(), "' AND season = 2025
    "))[[1]]
    
    true_prob <- 1 - ppois(input$line, lambda = avg)
    payout <- ifelse(input$odds > 0, input$odds, 100 / abs(input$odds) * 100)
    ev <- (true_prob * payout) - ((1 - true_prob) * 100)
    
    green_count <- sum(
      pillars()$workload %>% grepl("GREEN"),
      pillars()$matchup %>% grepl("GREEN"),
      pillars()$epa %>% grepl("GREEN"),
      pillars()$injury %>% grepl("GREEN")
    )
    
    output$pillars <- renderText({
      paste(
        pillars()$workload, "\n",
        pillars()$matchup, "\n",
        pillars()$epa, "\n",
        pillars()$injury
      )
    })
    
    output$result <- renderText({
      paste0(
        "TRUE PROB: ", round(true_prob*100,1), "%\n",
        "+EV: $", round(ev,2), " on $100\n",
        if(green_count >= 4) "BET NOW" else "PASS"
      )
    })
  })
  
  # LOG BET
  observeEvent(input$log, {
    log <- data.frame(
      Date = Sys.Date(),
      Player = input$player,
      Prop = input$prop,
      Line = input$line,
      Odds = input$odds,
      Amount = -input$bet_amount
    )
    write_csv(log, "../data/bet_log.csv", append = TRUE)
    showNotification("Bet logged!", type = "message")
  })
  
  # BANKROLL
  output$bankroll <- renderDT({
    if(file.exists("../data/sports_accounts.xlsx")) {
      read_excel("../data/sports_accounts.xlsx", sheet = "Summary")
    } else {
      data.frame(Status = "No bankroll file")
    }
  })
}

shinyApp(ui, server)