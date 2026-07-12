"""
run_ev_scan.py — WinWeave EV Scanner

HOW TO RUN:
    cd ~/WinWeave
    source .venv/bin/activate
    python run_ev_scan.py
"""

import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent))

from src.prop_analyzer import analyze_prop, print_full_analysis
from src.factors.prop_tracker import save_prediction

VALID_STATS = {
    "1": ("passing_yards",   "Passing Yards"),
    "2": ("rushing_yards",   "Rushing Yards"),
    "3": ("receiving_yards", "Receiving Yards"),
    "4": ("receptions",      "Receptions"),
    "5": ("passing_tds",     "Passing TDs"),
    "6": ("rushing_tds",     "Rushing TDs"),
    "7": ("receiving_tds",   "Receiving TDs"),
}


def run_demo():
    print("\n" + "="*65)
    print("  WinWeave EV Engine v2 — 8-Signal Demo (2024 wk14 context, recent-season stats)")
    print("="*65)

    examples = [
        dict(player_name="Josh Allen", stat="passing_yards",
             line=235.5, over_odds=-110, under_odds=-110,
             opponent="MIA", side="over", book="demo",
             season=2024, week=14),
        dict(player_name="Christian McCaffrey", stat="rushing_yards",
             line=79.5, over_odds=-115, under_odds=-105,
             opponent="DAL", side="over", book="demo",
             season=2024, week=14),
        dict(player_name="Tyreek Hill", stat="receiving_yards",
             line=89.5, over_odds=-110, under_odds=-110,
             opponent="NYJ", side="over", book="demo",
             season=2024, week=14),
    ]

    for ex in examples:
        name = ex["player_name"]
        print(f"\n  Analyzing {name}...")
        try:
            result = analyze_prop(**ex)
            print_full_analysis(result)
        except ValueError as e:
            print(f"  Skipped: {e}\n")
        except Exception as e:
            print(f"  Error: {e}\n")
            raise


def prompt_analysis():
    print("\n" + "="*65)
    print("  WinWeave — Manual Prop Entry")
    print("="*65)

    print("\n  Stat types:")
    for k, (_, label) in VALID_STATS.items():
        print(f"    {k}) {label}")

    player    = input("\n  Player name (exact): ").strip()
    stat_c    = input("  Stat type (1-7): ").strip()
    if stat_c not in VALID_STATS:
        print("Invalid stat choice."); return
    stat, _   = VALID_STATS[stat_c]

    try:
        line       = float(input("  Line: ").strip())
        over_odds  = int(input("  Over odds (e.g. -110): ").strip())
        under_odds = int(input("  Under odds (e.g. -110): ").strip())
    except ValueError:
        print("Invalid numbers."); return

    opponent  = input("  Opponent (e.g. CHI): ").strip().upper()
    side      = input("  Side (over/under): ").strip().lower()
    book      = input("  Book (e.g. fanduel): ").strip() or "manual"

    season_s  = input("  Season (e.g. 2025, or press Enter to skip): ").strip()
    week_s    = input("  Week (e.g. 5, or press Enter to skip): ").strip()
    season    = int(season_s) if season_s.isdigit() else None
    week      = int(week_s) if week_s.isdigit() else None

    print(f"\n  Analyzing {player} {stat} {side.upper()} {line}...")
    try:
        result = analyze_prop(
            player_name=player, stat=stat, line=line,
            over_odds=over_odds, under_odds=under_odds,
            opponent=opponent, side=side, book=book,
            season=season, week=week,
        )
        print_full_analysis(result)

        track = input("  Track this prediction for the feedback loop? (y/n): ").strip().lower()
        if track == "y":
            row_id = save_prediction(
                player_name=player, stat=stat, line=line, side=side,
                book=book, american_odds=result.american_odds,
                opponent=opponent, season=season or 0, week=week or 0,
                predicted_prob=result.true_probability,
                ev_percent=result.ev_percent,
                kelly_fraction=result.kelly_fraction,
                grade=result.grade(),
                sub_signals={
                    "hit_rate":      result.hit_rate_signal,
                    "model_prob":    result.model_signal,
                    "weather_mult":  result.weather_mult,
                    "roster_mult":   result.roster_mult,
                    "coaching_mult": result.coaching_mult,
                    "official_mult": result.official_mult,
                },
            )
            print(f"  Saved as prediction #{row_id}.")
            print(f"  After the game: python track_result.py --id {row_id} --value <actual>")
    except ValueError as e:
        print(f"\n  ERROR: {e}")
    except Exception as e:
        print(f"\n  Unexpected error: {e}")
        raise


def main():
    print("\nWinWeave EV Scanner v2")
    print("1) Run demo (3 historical examples)")
    print("2) Enter a prop manually")
    print("3) View model accuracy report")

    choice = input("\nChoice (1/2/3): ").strip()
    if choice == "1":
        run_demo()
    elif choice == "2":
        prompt_analysis()
    elif choice == "3":
        from src.factors.prop_tracker import model_accuracy_report
        print(model_accuracy_report())
    else:
        print("Invalid choice.")


if __name__ == "__main__":
    main()
