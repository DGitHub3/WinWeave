# WinWeave Database Schema Reference

This replaces `WINWEAVE_CHEAT_SHEET.txt` and `WINWEAVE_v2.0_CHEAT_SHEET`.
Those two files were mostly duplicates of each other, written as R
console commands. This version keeps only what's actually useful —
the schema itself — and shows Python usage instead, since that's
the direction the project is heading.

This is a reference, not a tutorial. When you need to write a new
query, check here first for table and column names before guessing.

---

## How to connect (Python)

```python
from src.db import get_connection
from src.queries import get_player_vs_opponent  # or write your own

with get_connection() as conn:
    cursor = conn.execute("SELECT * FROM props LIMIT 5")
```

Prefer using or extending the functions in `src/queries.py` over
writing raw SQL inline — keeps query logic in one place.

---

## Core Tables

### `props`
Player-level game stats. This is the main table for prop research.

| Column | Meaning |
|---|---|
| `player_id` | nflverse unique player ID |
| `player_display_name` | Full player name |
| `season`, `week` | When the game happened |
| `opponent_team` | Team code the player faced (e.g. `CHI`) |
| `passing_yards`, `rushing_yards`, `receiving_yards` | Core box score stats |
| `passing_tds` | Passing touchdowns |
| `passing_epa` | Expected Points Added on passing plays |

### `games`
Game-level schedule and results data, 1920–2025.

| Column | Meaning |
|---|---|
| `game_id` | Unique game identifier |
| `gameday` | Date of the game |
| `home_team`, `away_team` | Team codes |
| `home_score`, `away_score` | Final score |
| `season` | Season year |

### `rosters`
Player roster records, 1985–2025 (92k+ rows).

| Column | Meaning |
|---|---|
| `full_name` | Player name |
| `team`, `season` | Team and year |
| `position` | Position code |
| `jersey_number` | Jersey number |
| `status` | Roster status (active, IR, etc.) |

### `pbp`
Play-by-play data, 1999–2025 (1.25M+ rows). The deepest table —
use for granular workload and efficiency analysis.

| Column | Meaning |
|---|---|
| `play_id` | Unique play identifier |
| `play_type` | Run, pass, etc. |
| `yards_gained` | Yards on the play |
| `epa` | Expected Points Added |
| `passer_player_id` | player_id of the passer, if a pass play |

---

## Advanced Tables (added via add_advanced_data.R, v2.0)

| Table | What it's for | Key columns |
|---|---|---|
| `injuries` | Injury report history | `full_name`, `week`, `practice_status` |
| `snap_counts` | Workload / snap share | `player`, `week`, `offense_pct` |
| `depth_charts` | Team depth chart position | `team`, `position`, `depth_chart_position`, `player_display_name` |
| `next_gen_stats` | Separation, air yards | `player_display_name`, `week`, `avg_separation`, `avg_air_yards` |
| `player_stats` | Additional aggregated stats | varies |
| `combine` | NFL combine results | varies |
| `draft_picks` | Draft history | varies |
| `trades` | Trade history | varies |
| `contracts` | Contract data | `player`, `year_signed`, `value` |
| `officials` | Referee crew assignments | `referee`, game-level penalty data |

---

## Common Query Patterns

**Get a player's ID:**
```python
from src.queries import get_player_id
pid = get_player_id("Jalen Hurts")
```

**Player vs. a specific opponent (the "is this a good matchup" query):**
```python
from src.queries import get_player_vs_opponent
df = get_player_vs_opponent("DJ Moore", "CHI", limit=5)
```

**Full season stat line:**
```python
from src.queries import get_player_season
df = get_player_season("Jalen Hurts", 2025)
```

**Recent workload (snap %):**
```python
from src.queries import get_player_snap_pct
df = get_player_snap_pct("Jalen Hurts", limit=3)
```

**Injury status:**
```python
from src.queries import get_player_injury_status
df = get_player_injury_status("Jalen Hurts")
```

**Team schedule:**
```python
from src.queries import get_team_schedule
df = get_team_schedule("TB", 2025)
```

---

## Notes carried over from the old cheat sheets

- Player names must match exactly as stored (e.g. `"DJ Moore"` vs
  `"D.J. Moore"` may differ — check with `list_all_players()` in
  `src/queries.py` if a query returns nothing unexpectedly).
- 2025 PBP and injury data will be incomplete for weeks not yet played.
- Rebuild/refresh logic (the R `download_and_build.R` and
  `add_advanced_data.R` scripts) still works as-is for now — the
  database itself isn't changing, only how we query it.
