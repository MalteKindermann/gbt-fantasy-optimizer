"""
CLI: predict the win probability between two beach-volleyball teams.

Usage:
    python -m scripts.elo.predict --team1 Henning Wuest --team2 Just Pfretzschner

Names are matched against `current_ratings` in `data/elo_ratings.db` using the
same normalisation as the builder (lowercased, accent-stripped surname).
If a name matches multiple players (e.g. "Wuest" → Lui / Tamo / Filo), the
prediction picks the one with the most matches played and warns about the
ambiguity.
"""
from __future__ import annotations

import argparse
import sqlite3
import sys
import unicodedata
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from _env import data_dir  # noqa: E402
from elo import elo as elo_math  # noqa: E402


DB_PATH = data_dir() / "elo_ratings.db"


def _norm(s: str) -> str:
    s = unicodedata.normalize("NFKD", s or "")
    s = "".join(c for c in s if not unicodedata.combining(c))
    return s.strip().lower()


def resolve(conn: sqlite3.Connection, query: str) -> tuple[str, float, int, str]:
    """
    Resolve a free-form name to (entity_id, elo, matches_played, last_active).

    Matches against the lowercased "lastname_firstname" id form.  If the query
    contains a space, treat as "Firstname Lastname"; else surname-only.
    """
    q = _norm(query)
    # Exact id match first
    row = conn.execute("""
        SELECT entity_id, elo, matches_played, last_active
        FROM current_ratings WHERE entity_kind='individual' AND entity_id=?
        LIMIT 1;
    """, (q,)).fetchone()
    if row:
        return row

    # Try "Lastname Firstname" or "Firstname Lastname"
    parts = q.split()
    candidates: list[tuple[str, float, int, str]] = []
    if len(parts) >= 2:
        last = parts[-1]
        first = " ".join(parts[:-1])
        eid = f"{last}_{first}"
        row = conn.execute("""
            SELECT entity_id, elo, matches_played, last_active
            FROM current_ratings WHERE entity_kind='individual' AND entity_id=?
            LIMIT 1;
        """, (eid,)).fetchone()
        if row:
            return row
        # Reverse order
        eid = f"{first}_{last}"
        row = conn.execute("""
            SELECT entity_id, elo, matches_played, last_active
            FROM current_ratings WHERE entity_kind='individual' AND entity_id=?
            LIMIT 1;
        """, (eid,)).fetchone()
        if row:
            return row

    # Surname-prefix fuzzy match, pick the one with most matches
    surname = parts[-1] if parts else q
    rows = conn.execute("""
        SELECT entity_id, elo, matches_played, last_active
        FROM current_ratings WHERE entity_kind='individual'
        AND entity_id LIKE ? || '_%'
        ORDER BY matches_played DESC;
    """, (surname,)).fetchall()
    candidates.extend(rows)
    if not candidates:
        raise SystemExit(f"No player matched '{query}'.")
    if len(candidates) > 1:
        names = ", ".join(c[0] for c in candidates[:5])
        print(f"  ! ambiguous '{query}' -> {names}  (picking '{candidates[0][0]}')",
              file=sys.stderr)
    return candidates[0]


def confidence(min_matches: int, avg_matches: float) -> str:
    if min_matches < 10:
        return "low"
    if avg_matches < 30:
        return "medium"
    return "high"


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--team1", nargs=2, metavar=("PLAYER1", "PLAYER2"),
                    required=True)
    ap.add_argument("--team2", nargs=2, metavar=("PLAYER1", "PLAYER2"),
                    required=True)
    args = ap.parse_args()

    if not DB_PATH.exists():
        raise SystemExit("Build the DB first: "
                         "python -m scripts.elo.build_ratings --phase build")
    conn = sqlite3.connect(DB_PATH)

    p1a = resolve(conn, args.team1[0])
    p1b = resolve(conn, args.team1[1])
    p2a = resolve(conn, args.team2[0])
    p2b = resolve(conn, args.team2[1])

    # Team rating
    def _team(pid_a: str, pid_b: str) -> tuple[float, int]:
        tid = elo_math.team_key(pid_a, pid_b)
        row = conn.execute("""
            SELECT elo, matches_played FROM current_ratings
            WHERE entity_kind='team' AND entity_id=?
            LIMIT 1;
        """, (tid,)).fetchone()
        return row if row else (None, 0)

    t1_elo, t1_n = _team(p1a[0], p1b[0])
    t2_elo, t2_n = _team(p2a[0], p2b[0])

    cfg = elo_math.EloConfig()
    blend1 = elo_math.blended(p1a[1], p1b[1], t1_elo, t1_n, cfg)
    blend2 = elo_math.blended(p2a[1], p2b[1], t2_elo, t2_n, cfg)
    p1_wins = elo_math.expected(blend1, blend2)

    matches = [p1a[2], p1b[2], p2a[2], p2b[2]]
    conf = confidence(min(matches), sum(matches) / 4)

    print()
    print(f"Team 1: {p1a[0]} / {p1b[0]}")
    print(f"  ELO_ind:      {p1a[1]:.0f} ({p1a[2]} matches) "
          f"+ {p1b[1]:.0f} ({p1b[2]} matches)")
    print(f"  ELO_team:     "
          + (f"{t1_elo:.0f} ({t1_n} matches together)" if t1_elo else "no team history"))
    print(f"  ELO_combined: {blend1:.0f}")
    print()
    print(f"Team 2: {p2a[0]} / {p2b[0]}")
    print(f"  ELO_ind:      {p2a[1]:.0f} ({p2a[2]} matches) "
          f"+ {p2b[1]:.0f} ({p2b[2]} matches)")
    print(f"  ELO_team:     "
          + (f"{t2_elo:.0f} ({t2_n} matches together)" if t2_elo else "no team history"))
    print(f"  ELO_combined: {blend2:.0f}")
    print()
    print(f"Win prob Team 1: {p1_wins:.1%}")
    print(f"Win prob Team 2: {1.0 - p1_wins:.1%}")
    print(f"Confidence: {conf}  "
          f"(min {min(matches)} matches, avg {sum(matches)/4:.0f})")


if __name__ == "__main__":
    main()
