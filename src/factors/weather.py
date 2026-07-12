"""
src/factors/weather.py — Weather Impact Factor

Passing stats are significantly affected by weather.
Research shows:
  Wind >15 mph:  ~8% reduction in passing yards/TDs
  Wind >25 mph:  ~18% reduction
  Temp <32°F:    ~5% reduction in passing yards
  Rain/snow:     ~10% reduction in passing yards

Rushing stats are less affected but still relevant in extreme cold.

Data source: games table (nflverse includes temp, wind, weather columns).
For LIVE/upcoming games: uses Open-Meteo free weather API (no key needed).
"""

import sqlite3
import math
from pathlib import Path
from typing import Optional

DB_PATH = Path(__file__).resolve().parent.parent.parent / "data" / "winweave.db"

# Passing stats most affected by weather
PASSING_STATS = {"passing_yards", "passing_tds", "receiving_yards",
                 "receiving_tds", "receptions", "targets"}
RUSHING_STATS = {"rushing_yards", "rushing_tds"}


def get_game_weather(game_id: str) -> dict:
    """
    Gets weather data for a specific historical game from the games table.
    Returns a dict with temp, wind, and weather description.
    Returns None if game not found or weather data missing.
    """
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    try:
        # nflverse games table uses: temp, wind, weather
        # Try multiple possible column names for resilience
        row = conn.execute("""
            SELECT * FROM games WHERE game_id = ? LIMIT 1
        """, (game_id,)).fetchone()

        if not row:
            return {}

        # Extract weather fields — handle column name variations
        keys = row.keys()
        result = {}

        for temp_col in ["temp", "temperature", "weather_temp"]:
            if temp_col in keys and row[temp_col] is not None:
                result["temp"] = float(row[temp_col])
                break

        for wind_col in ["wind", "wind_speed", "weather_wind"]:
            if wind_col in keys and row[wind_col] is not None:
                result["wind"] = float(row[wind_col])
                break

        for wx_col in ["weather", "weather_detail", "weather_type"]:
            if wx_col in keys and row[wx_col] is not None:
                result["weather_text"] = str(row[wx_col]).lower()
                break

        # Dome/indoor check — weather irrelevant for dome games
        for roof_col in ["roof", "stadium_type"]:
            if roof_col in keys and row[roof_col] is not None:
                roof = str(row[roof_col]).lower()
                if any(x in roof for x in ["dome", "closed", "retractable"]):
                    result["is_dome"] = True
                    break

        return result
    finally:
        conn.close()


def get_weather_for_team_game(team: str, season: int,
                               week: int) -> dict:
    """
    Gets weather for a team's game in a specific season/week.
    More useful than game_id for analysis.
    """
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    try:
        row = conn.execute("""
            SELECT game_id FROM games
            WHERE (home_team = ? OR away_team = ?)
              AND season = ? AND week = ?
            LIMIT 1
        """, (team, team, season, week)).fetchone()

        if not row:
            return {}

        return get_game_weather(row["game_id"])
    finally:
        conn.close()


def fetch_forecast_weather(lat: float, lon: float,
                           game_date: str) -> dict:
    """
    Gets LIVE weather forecast from Open-Meteo (free, no API key).
    Use this for upcoming games.

    game_date format: "2025-09-14"

    Returns dict with temp (°F), wind (mph), precip.
    """
    try:
        import urllib.request
        import json

        url = (
            f"https://api.open-meteo.com/v1/forecast?"
            f"latitude={lat}&longitude={lon}"
            f"&daily=temperature_2m_max,windspeed_10m_max,precipitation_sum"
            f"&temperature_unit=fahrenheit&windspeed_unit=mph"
            f"&timezone=America%2FChicago&start_date={game_date}"
            f"&end_date={game_date}"
        )

        with urllib.request.urlopen(url, timeout=5) as resp:
            data = json.loads(resp.read())

        daily = data.get("daily", {})
        return {
            "temp":   daily.get("temperature_2m_max", [None])[0],
            "wind":   daily.get("windspeed_10m_max",  [None])[0],
            "precip": daily.get("precipitation_sum",  [None])[0],
        }
    except Exception:
        return {}  # Network error, API down — return empty, caller handles gracefully


def weather_multiplier(weather: dict, stat: str) -> float:
    """
    Calculates how much weather should adjust our probability estimate.

    Returns a multiplier:
      1.00 = neutral (dome, mild weather)
      0.85 = significant negative impact (high wind or cold)
      0.75 = severe impact (snow + wind)
      1.05 = slight positive (slight tailwind, but we cap upside)

    Rushing stats are much less affected than passing stats.
    """
    if not weather:
        return 1.0  # No data — assume neutral

    # Dome games — weather irrelevant
    if weather.get("is_dome"):
        return 1.0

    is_passing = stat in PASSING_STATS
    is_rushing = stat in RUSHING_STATS

    multiplier = 1.0

    # Wind impact (strongest factor)
    wind = weather.get("wind", 0) or 0
    if is_passing:
        if wind > 25:
            multiplier *= 0.80  # severe wind
        elif wind > 20:
            multiplier *= 0.88
        elif wind > 15:
            multiplier *= 0.93
        elif wind > 10:
            multiplier *= 0.97
    elif is_rushing:
        # Wind matters less for rushing
        if wind > 25:
            multiplier *= 0.96

    # Temperature impact
    temp = weather.get("temp", 65) or 65
    if is_passing:
        if temp < 20:
            multiplier *= 0.90
        elif temp < 32:
            multiplier *= 0.95
        elif temp < 40:
            multiplier *= 0.98

    # Precipitation (rain/snow)
    precip = weather.get("precip", 0) or 0
    weather_text = weather.get("weather_text", "")
    is_wet = (precip and precip > 0.1) or \
             any(x in weather_text for x in ["rain", "snow", "sleet"])

    if is_wet and is_passing:
        multiplier *= 0.92
    elif is_wet and is_rushing:
        multiplier *= 0.97

    # Cap the range
    return max(0.70, min(1.05, multiplier))


def describe_weather_impact(weather: dict, stat: str) -> str:
    """Human-readable description of weather impact."""
    mult = weather_multiplier(weather, stat)
    wind = weather.get("wind", 0) or 0
    temp = weather.get("temp", 65) or 65

    if weather.get("is_dome"):
        return "Dome — weather neutral"
    if not weather:
        return "Weather data unavailable"

    desc = f"Temp: {temp:.0f}°F, Wind: {wind:.0f}mph"
    if mult < 0.80:
        return f"{desc} — SEVERE impact ({mult:.0%} adjustment)"
    elif mult < 0.90:
        return f"{desc} — significant impact ({mult:.0%} adjustment)"
    elif mult < 0.97:
        return f"{desc} — mild impact ({mult:.0%} adjustment)"
    else:
        return f"{desc} — minimal impact"
