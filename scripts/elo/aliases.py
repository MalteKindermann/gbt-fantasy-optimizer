"""
Heuristic player-name aliasing across DVV / FIVB / bvbinfo.

The goal: merge variant spellings of the same person so the rating models
see more data per player. Examples that should merge:
  - "Max Just" (DVV) + "Maximilian Just" (FIVB)
  - "Chris Mc Hugh" (bvb) + "Christopher McHugh" (FIVB)

The user's original spec was (lastname + birthdate + country). Realistically
DVV exposes no birthdate, so we fall back to a tiered heuristic:

  1. Birthdate match (lastname-equal, same DoB): high confidence.
  2. Prefix match on firstname + same country (DVV implied "Germany"):
     mid confidence.  E.g. "max" ⊆ "maximilian".
  3. Single-occurrence anchors (only one carrier of that lastname): no
     aliasing needed.

Output is written to `data/elo_aliases.json`. The user can hand-edit
`data/elo_aliases_overrides.json` to force or block specific merges; that
file is merged on top of the auto-generated one with the highest priority.

Loaded by build_ratings._consolidate_records at the end of the pipeline,
remapping every (player1a/1b/player2a/2b) id through the alias table.
"""
from __future__ import annotations

import json
import sys
import unicodedata
from collections import defaultdict
from pathlib import Path
from typing import Iterable

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from _env import data_dir, load_dotenv_files  # noqa: E402
load_dotenv_files()

from elo import scraper as sc  # noqa: E402
from elo import scraper_bvb as bvb  # noqa: E402

DATA = data_dir()
ALIASES_PATH = DATA / "elo_aliases.json"
OVERRIDES_PATH = DATA / "elo_aliases_overrides.json"


# ── Name helpers ──────────────────────────────────────────────────────────────

def _normalise(s: str) -> str:
    if not s:
        return ""
    s = unicodedata.normalize("NFKD", s.strip())
    s = "".join(c for c in s if not unicodedata.combining(c))
    return " ".join(s.lower().split())


def player_id_from_name(first: str, last: str) -> str:
    return f"{_normalise(last)}_{_normalise(first)}".strip("_")


def _split_full(name: str) -> tuple[str, str]:
    parts = (name or "").strip().split()
    if not parts:
        return ("", "")
    if len(parts) == 1:
        return ("", parts[0])
    return (parts[0], " ".join(parts[1:]))


# ── Candidate collection ─────────────────────────────────────────────────────

def _collect_dvv_candidates() -> list[dict]:
    out: list[dict] = []
    for p in sorted(DATA.glob("players_season_*.json")):
        try:
            doc = json.loads(p.read_text(encoding="utf-8"))
        except Exception:
            continue
        pl = (doc.get("fields") or {}).get("pl") or {}
        pl = (pl.get("mapValue") or {}).get("fields") or {}
        for _pid, node in pl.items():
            f = (node.get("mapValue") or {}).get("fields") or {}
            fn = (f.get("fn") or {}).get("stringValue", "")
            ln = (f.get("ln") or {}).get("stringValue", "")
            if not ln:
                continue
            out.append({
                "src": "dvv", "first": fn, "last": ln,
                "country": "Germany", "birthdate": None,
                "n_played": 0,
            })
    return out


def _collect_fivb_candidates() -> list[dict]:
    out: list[dict] = []
    csv_path = sc.FIVB_CSV_PATH
    if not csv_path.exists():
        return out
    counts: dict[tuple, dict] = {}
    for row in sc.iter_fivb_rows(csv_path):
        for tag in ("w_player1", "w_player2", "l_player1", "l_player2"):
            full = (row.get(tag) or "").strip()
            if not full:
                continue
            first, last = _split_full(full)
            if not last:
                continue
            prefix = tag[:4]  # 'w_p1' etc.
            country = (row.get(f"{prefix}_country") or "").strip()
            bdate   = (row.get(f"{prefix}_birthdate") or "").strip() or None
            key = (_normalise(last), _normalise(first), country)
            ent = counts.get(key)
            if ent is None:
                counts[key] = {
                    "src": "fivb", "first": first, "last": last,
                    "country": country, "birthdate": bdate, "n_played": 1,
                }
            else:
                ent["n_played"] += 1
                if not ent.get("birthdate") and bdate:
                    ent["birthdate"] = bdate
    out.extend(counts.values())
    return out


def _collect_bvb_candidates() -> list[dict]:
    """Walk cached bvb match results. Country is per-team from the match."""
    out: list[dict] = []
    discovered = DATA / "raw" / "bvb" / "_discovered.json"
    if not discovered.exists():
        return out
    refs = json.loads(discovered.read_text(encoding="utf-8"))
    counts: dict[tuple, dict] = {}
    for ref in refs:
        ms = bvb.fetch_tournament_matches(ref["tournament_id"], ref["year"])
        for m in ms:
            for full, country in (
                *((p, m.team_w_country) for p in m.team_w_players),
                *((p, m.team_l_country) for p in m.team_l_players),
            ):
                first, last = _split_full(full)
                if not last:
                    continue
                key = (_normalise(last), _normalise(first), country or "")
                ent = counts.get(key)
                if ent is None:
                    counts[key] = {
                        "src": "bvb", "first": first, "last": last,
                        "country": country or "", "birthdate": None,
                        "n_played": 1,
                    }
                else:
                    ent["n_played"] += 1
    out.extend(counts.values())
    return out


# ── Merge cluster building ───────────────────────────────────────────────────

_DE_LIKE = {"germany", "deutschland", "ger", "de", "deu"}


def _country_compatible(a: str, b: str) -> bool:
    a = (a or "").strip().lower()
    b = (b or "").strip().lower()
    if not a or not b:
        return True
    if a == b:
        return True
    if a in _DE_LIKE and b in _DE_LIKE:
        return True
    return False


def _first_name_compatible(a: str, b: str) -> tuple[bool, str]:
    """Returns (compatible, reason). Compatible if exact equal OR prefix
    match with at least 3 chars."""
    na, nb = _normalise(a), _normalise(b)
    if not na or not nb:
        return (False, "")
    if na == nb:
        return (True, "exact")
    short, long = (na, nb) if len(na) < len(nb) else (nb, na)
    if len(short) >= 3 and long.startswith(short):
        return (True, "prefix")
    return (False, "")


def _canonical_full(a: dict, b: dict) -> dict:
    """Pick the entry that should be the canonical id:
    longer firstname wins (Maximilian > Max), then higher n_played."""
    la, lb = len(a.get("first") or ""), len(b.get("first") or "")
    if la != lb:
        return a if la > lb else b
    if a.get("n_played", 0) != b.get("n_played", 0):
        return a if a["n_played"] > b["n_played"] else b
    return a


def build_merges(candidates: list[dict]) -> dict:
    """Returns a dict ready to serialise to elo_aliases.json."""
    # Group by normalised last name
    by_last: dict[str, list[dict]] = defaultdict(list)
    for c in candidates:
        by_last[_normalise(c["last"])].append(c)

    merges: list[dict] = []
    ignored: list[dict] = []

    for last, group in by_last.items():
        if len(group) <= 1:
            continue
        # Walk all pairs (small groups, n^2 fine in practice)
        # Build clusters: union-find by compatible pairs
        parent = list(range(len(group)))

        def find(i: int) -> int:
            while parent[i] != i:
                parent[i] = parent[parent[i]]
                i = parent[i]
            return i

        def union(i: int, j: int) -> None:
            ri, rj = find(i), find(j)
            if ri != rj:
                parent[ri] = rj

        pair_reasons: dict[tuple[int, int], tuple[str, str]] = {}

        for i in range(len(group)):
            for j in range(i + 1, len(group)):
                a, b = group[i], group[j]
                # Same record (same src + same first + same country) — keep separate
                if (a["src"] == b["src"]
                        and _normalise(a["first"]) == _normalise(b["first"])
                        and (a.get("country") or "") == (b.get("country") or "")):
                    continue
                # Tier 1 — birthdate match (overrides everything)
                if a.get("birthdate") and b.get("birthdate") \
                        and a["birthdate"] == b["birthdate"]:
                    union(i, j)
                    pair_reasons[(i, j)] = ("birthdate_match", "high")
                    continue
                # Tier 2 — firstname prefix-compatible + country-compatible
                ok, why = _first_name_compatible(a["first"], b["first"])
                if not ok:
                    continue
                if not _country_compatible(a.get("country", ""),
                                           b.get("country", "")):
                    ignored.append({
                        "last": last,
                        "a": {"src": a["src"], "first": a["first"],
                              "country": a["country"]},
                        "b": {"src": b["src"], "first": b["first"],
                              "country": b["country"]},
                        "reason": "country_mismatch",
                    })
                    continue
                union(i, j)
                tag = ("exact_country_match" if why == "exact"
                       else "prefix_country_match")
                conf = "high" if why == "exact" else "medium"
                pair_reasons[(i, j)] = (tag, conf)

        # Build clusters
        clusters: dict[int, list[int]] = defaultdict(list)
        for idx in range(len(group)):
            clusters[find(idx)].append(idx)

        for root, idxs in clusters.items():
            if len(idxs) < 2:
                continue
            members = [group[i] for i in idxs]
            canon = members[0]
            for m in members[1:]:
                canon = _canonical_full(canon, m)
            canon_id = player_id_from_name(canon["first"], canon["last"])
            alt_ids = sorted({
                player_id_from_name(m["first"], m["last"])
                for m in members
                if player_id_from_name(m["first"], m["last"]) != canon_id
            })
            if not alt_ids:
                continue
            # Pick the strongest reason/confidence across pairs in this cluster
            reason, conf = "prefix_country_match", "medium"
            for (i, j), (r, c) in pair_reasons.items():
                if i in idxs and j in idxs:
                    if c == "high":
                        reason, conf = r, c
            merges.append({
                "canonical": canon_id,
                "alternatives": alt_ids,
                "reason": reason,
                "confidence": conf,
                "members": [
                    {"src": m["src"], "first": m["first"], "last": m["last"],
                     "country": m.get("country", ""),
                     "n_played": m.get("n_played", 0)}
                    for m in members
                ],
            })

    merges.sort(key=lambda m: (m["confidence"] != "high", m["canonical"]))
    return {
        "generated_at": "",  # ScheduleWakeup-style stamping done at caller
        "merges": merges,
        "ignored_collisions": ignored,
    }


# ── Public API ───────────────────────────────────────────────────────────────

def write_aliases_file() -> Path:
    candidates: list[dict] = []
    candidates += _collect_dvv_candidates()
    candidates += _collect_fivb_candidates()
    candidates += _collect_bvb_candidates()
    doc = build_merges(candidates)
    ALIASES_PATH.parent.mkdir(parents=True, exist_ok=True)
    ALIASES_PATH.write_text(json.dumps(doc, indent=2, ensure_ascii=False),
                            encoding="utf-8")
    return ALIASES_PATH


def load_alias_map() -> dict[str, str]:
    """Return {alternative_id: canonical_id} suitable for player-id remapping.

    Merges the auto-generated file with the user override file. The override
    wins on conflict — and additionally can contain a top-level "block" list
    of canonical ids whose merges should be undone.
    """
    mapping: dict[str, str] = {}
    blocked: set[str] = set()

    def _ingest(doc: dict, allow_block: bool) -> None:
        for m in doc.get("merges", []):
            canon = m.get("canonical")
            if not canon:
                continue
            for alt in m.get("alternatives", []):
                if alt and alt != canon:
                    mapping[alt] = canon
        if allow_block:
            for b in doc.get("block", []):
                blocked.add(b)

    if ALIASES_PATH.exists():
        try:
            _ingest(json.loads(ALIASES_PATH.read_text(encoding="utf-8")),
                    allow_block=False)
        except Exception:
            pass
    if OVERRIDES_PATH.exists():
        try:
            _ingest(json.loads(OVERRIDES_PATH.read_text(encoding="utf-8")),
                    allow_block=True)
        except Exception:
            pass

    # Apply user blocks: drop any mapping whose alternative OR canonical is blocked
    if blocked:
        mapping = {k: v for k, v in mapping.items()
                   if k not in blocked and v not in blocked}

    # Transitive close: A -> B -> C ⇒ A -> C
    for k in list(mapping.keys()):
        seen = {k}
        v = mapping[k]
        while v in mapping and v not in seen:
            seen.add(v)
            v = mapping[v]
        mapping[k] = v
    return mapping


def apply_aliases(records: list[dict], mapping: dict[str, str]) -> int:
    """In-place remap player ids in match records. Returns number of fields
    rewritten (counted across all 4 slots × all matches)."""
    if not mapping:
        return 0
    n = 0
    for r in records:
        for k in ("player1a", "player1b", "player2a", "player2b"):
            pid = r.get(k)
            if pid and pid in mapping:
                r[k] = mapping[pid]
                n += 1
        # Recompute team ids so team1_id/team2_id remain stable hashes of the
        # (possibly-remapped) player pair. Import lazily to avoid hard dep.
        try:
            from elo import elo as _elo_math
            r["team1_id"] = _elo_math.team_key(r["player1a"], r["player1b"])
            r["team2_id"] = _elo_math.team_key(r["player2a"], r["player2b"])
        except Exception:
            pass
    return n


# ── CLI ──────────────────────────────────────────────────────────────────────

def main() -> None:
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("--print", action="store_true",
                    help="print summary of merges to stdout")
    args = ap.parse_args()
    p = write_aliases_file()
    doc = json.loads(p.read_text(encoding="utf-8"))
    high = sum(1 for m in doc["merges"] if m["confidence"] == "high")
    med  = sum(1 for m in doc["merges"] if m["confidence"] == "medium")
    print(f"[aliases] wrote {p} — {len(doc['merges'])} merges "
          f"({high} high, {med} medium), "
          f"{len(doc['ignored_collisions'])} ignored")
    if args.print:
        for m in doc["merges"][:30]:
            alts = ", ".join(m["alternatives"])
            print(f"  [{m['confidence']:<6}] {m['canonical']:<30}  <- {alts}  "
                  f"({m['reason']})")


if __name__ == "__main__":
    main()
