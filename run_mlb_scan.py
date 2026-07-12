"""
run_mlb_scan.py — WinWeave MLB Prop Scanner

Mirrors run_ev_scan.py's interaction pattern (demo mode + manual
entry) so the two sports feel consistent to use side by side. Both
scanners share the exact same math core (src/ev_engine.py) — only
the data layer differs per sport.

PREREQUISITE:
    python scrapers/build_mlb_db.py   (builds the MLB tables — takes
                                        roughly 15-30 minutes on first run)

HOW TO RUN:
    cd ~/WinWeave
    source .venv/bin/activate
    python run_mlb_scan.py
"""

import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent))

from src.mlb_analyzer import analyze_mlb_prop, print_mlb_analysis, MLB_STATS


def run_demo():
    """
    Demo using real star players, IF your MLB database has been
    built. If not, this will tell you clearly to run
    scrapers/build_mlb_db.py first — same pattern as the NFL demo.
    """
    print("\n" + "="*62)
    print("  WinWeave MLB Scanner — Demo")
    print("  (Uses whatever games are currently in your database)")
    print("="*62)

    examples = [
        dict(player_name="Aaron Judge", stat="hits", line=0.5,
             over_odds=-165, under_odds=+135,
             opponent="Boston Red Sox", side="over", book="demo",
             is_home=True),
        dict(player_name="Shohei Ohtani", stat="total_bases", line=1.5,
             over_odds=-120, under_odds=-105,
             opponent="San Diego Padres", side="over", book="demo",
             is_home=False),
        dict(player_name="Gerrit Cole", stat="strikeouts", line=5.5,
             over_odds=-110, under_odds=-115,
             opponent="Boston Red Sox", side="over", book="demo",
             is_home=True),
    ]

    ran_any = False
    for ex in examples:
        print(f"\n  Analyzing {ex['player_name']}...")
        try:
            result = analyze_mlb_prop(**ex)
            print_mlb_analysis(result)
            ran_any = True
        except ValueError as e:
            print(f"  Skipped: {e}\n")

    if not ran_any:
        print("\n  No demo examples could run — your MLB tables are "
              "likely empty.")
        print("  Run this first:  python scrapers/build_mlb_db.py\n")


def prompt_analysis():
    print("\n" + "="*62)
    print("  WinWeave — MLB Manual Prop Entry")
    print("="*62)

    stats = list(MLB_STATS.keys())
    print("\n  Stat types:")
    for i, s in enumerate(stats, 1):
        print(f"    {i}) {s.replace('_',' ').title()}")

    player = input("\n  Player name (exact, e.g. 'Aaron Judge'): ").strip()
    choice = input(f"  Stat (1-{len(stats)}): ").strip()
    try:
        stat = stats[int(choice) - 1]
    except (ValueError, IndexError):
        print("  Invalid stat choice.")
        return

    try:
        line       = float(input("  Line (e.g. 1.5): ").strip())
        over_odds  = int(input("  Over odds (e.g. -115): ").strip())
        under_odds = int(input("  Under odds (e.g. -105): ").strip())
    except ValueError:
        print("  Invalid numbers.")
        return

    opponent = input("  Opponent team name (e.g. 'Boston Red Sox'): ").strip()
    side     = input("  Side (over/under): ").strip().lower()
    if side not in ("over", "under"):
        print("  Must be over or under.")
        return

    home_s  = input("  Is the player at home? (y/n/skip): ").strip().lower()
    is_home = True if home_s == "y" else False if home_s == "n" else None
    starter = input("  Opposing starting pitcher (exact name, or press "
                    "Enter to skip): ").strip() or None
    book    = input("  Book (e.g. fanduel): ").strip() or "manual"

    try:
        r = analyze_mlb_prop(
            player_name=player, stat=stat, line=line,
            over_odds=over_odds, under_odds=under_odds,
            opponent=opponent, side=side, book=book, is_home=is_home,
            opposing_starter=starter,
        )
        print_mlb_analysis(r)
    except ValueError as e:
        print(f"\n  ERROR: {e}")


def main():
    print("\nWinWeave MLB Scanner")
    print("1) Run demo (real star players, if DB is built)")
    print("2) Enter a prop manually")

    choice = input("\nChoice (1 or 2): ").strip()
    if choice == "1":
        run_demo()
    elif choice == "2":
        prompt_analysis()
    else:
        print("Invalid choice.")


if __name__ == "__main__":
    main()
