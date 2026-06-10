# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

A static-frontend (vanilla JS) + Python-backend tool that picks an optimal Fantasy team for the German Beach Tour. There is **no build step** — `app.js` is loaded directly by the browser. The Python side scrapes external data (DVV rankings, GBT bracket, H2H) and serves both the static files and a small JSON API.

> Note: The top-level `README.md` is **outdated** (describes the old `data.js`/Greedy era). This file is the source of truth.

## Running

```bash
# Install Python deps
pip install -r scripts/requirements.txt

# Start the dev server (drop-in replacement for `python -m http.server`)
python scripts/serve.py            # serves on :8000
python scripts/serve.py 8123       # on a different port

# Run the simulator manually (also runs implicitly via the server's /api/simulate)
python scripts/simulate_tournament.py --gender m
python scripts/simulate_tournament.py --gender f
python scripts/simulate_tournament.py --gender m --simulations 50000 --force-refresh
```

**Always use `serve.py`, not `python -m http.server`.** The static server has no API endpoints — clicking "Prognose neu" or the price/ambiguity pickers will return 404.

After Python code changes you must restart `serve.py` (Ctrl+C → re-run); Python doesn't hot-reload. After frontend changes the browser also needs a hard reload (Ctrl+Shift+R) — `serve.py` already sends `Cache-Control: no-store` for `.js`/`.html`/`.css`/`.json`, so a normal reload works after the first hard reload.

There are no tests, no linter, no build, no package.json scripts.

## Data flow (the big picture)

```
                ┌─────────────────────────────────────────────┐
                │  External sources                           │
                │  • beach.volleyball-verband.de (DVV)        │
                │      tur.php → tur-sl.php (Setzliste)       │
                │      tur-sp.php (Spielplan, gespielte Spiele)│
                │      rl-show.php (DVV-Ranglisten)           │
                │  • gbt.hanski.de (bracket fallback, H2H)    │
                │  • firestore (gbt-fantasy.web.app, Preise)  │
                └──────────┬──────────────────────────────────┘
                           │ scraped
                           ▼
       ┌─────────────────────────────────────────┐
       │ scripts/simulate_tournament.py          │
       │  • Disk cache → data/.cache/*.json      │
       │  • Auto-syncs players_available.json    │
       │  • Monte-Carlo + deterministic predict  │
       │  • Writes data/tournament_sim.json      │
       └──────────┬──────────────────────────────┘
                  │ called via /api/simulate
                  ▼
       ┌─────────────────────────────────────────┐
       │ scripts/serve.py                         │
       │  static files + API endpoints           │
       └──────────┬──────────────────────────────┘
                  │ HTTP
                  ▼
              app.js (browser)
```

## Files that matter

| Path | Purpose |
|---|---|
| `index.html` | Four tabs: 📊 Alle Spieler · 🔒 Meine Picks · ⚖ Vergleich · 🏆 Turnier-Baum. The filter bar (`.player-filters` — pos/gender/status/price-range/sort) is injected by JS into BOTH the Alle-Spieler and Meine-Picks tabs from a shared `playerFilters` global; changes mirror across both. A top-level `🆚 H2H Vergleich` button opens an ad-hoc two-player comparator. |
| `app.js` | Single-file frontend. State held in module-level `let`. Renders into existing tab-content divs. |
| `styles.css` | Hand-written, no preprocessor. Dark theme with CSS custom properties under `:root`. |
| `scripts/simulate_tournament.py` | Scraping, caching, sync, Monte-Carlo, deterministic prediction, JSON output. |
| `scripts/serve.py` | `ThreadingTCPServer` subclass. Imports `simulate_tournament` to handle the simulate endpoint inline. |
| `scripts/firestore_sync.py` | Fetches the gbt-fantasy.web.app Firestore season-doc via refresh-token auth. Source of truth for prices and active-season player list. |
| `scripts/dvv_tournament.py` | **Primary bracket source.** Scrapes `beach.volleyball-verband.de/public/`: discovers the next/current "Deutsche Beach-Volleyball Tour\\German Beach Tour" tournament per gender, parses Setzliste + Spielplan, and emits a bracket-dict in the legacy gbt.hanski.de schema (plus `meta.source='dvv'` and `matches[]` for already-played games). `fetch_tournament_bracket()` in `simulate_tournament.py` tries this first; if it fails or the tournament size doesn't fit the 8-team template, the gbt.hanski.de fallback is used. |
| `data/firebase_auth.json` | **User-provided, gitignored.** `{apiKey, refreshToken, ...}` from the one-time browser-console snippet. Refresh-token lives until user actively logs out. |
| `fetch_auth_token.txt` | Browser-console snippet that produces `firebase_auth.json` (run once on `https://gbt-fantasy.web.app/` after login). |
| `data/players_all.json` | Full season database — **edit manually** for `pos`/`tp`/`t`/`mp`. Position is not auto-fetched. New players with no GBT history get `tp:0`. |
| `data/players_available.json` | Auto-synced from current brackets on every sim run. New players land with `price: -1`. User price/name choices are preserved on resync. |
| `data/tournament_sim.json` | Sim output. Keyed by `byGender.m` and `byGender.f`; `playerExpectedMatches` at top level is the merged across genders. |
| `data/bracket_{m,f}.json` | **Optional fallback**; normally auto-fetched from `https://gbt.hanski.de/rechner/data/bracket_{m,f}.json`. |
| `data/.cache/` | Disk cache: DVV (TTL 1 h) / GBT bracket (1 h) / H2H per-pair (24 h). Wipe or pass `--force-refresh` to refresh. |
| `fetch_new_data.txt` | Browser-console snippet to re-export the player base JSON from the GBT app. |

## Backend API (`scripts/serve.py`)

- `GET /api/sim-status` → `{exists, fresh, age_s, playersHashMatch, tournaments}`. "Fresh" = players-list hash matches AND age < 6 h.
- `POST /api/simulate?gender=m|f|all&force=1&qualifiers=1` → blocks until done, single-flight via `_sim_lock`. Returns `{ok, duration_s, status}`.
- `POST /api/swap-player {from, to}` → renames an entry in `players_available.json` (preserves price). If `to` already exists in the list (two bracket slots share a last name and the user wants to switch which full name goes where), performs a **true swap** instead of a rename — both `players_available.json` and `syncInfo.ambiguous[*].chosen` are swapped so the other slot keeps its own identity. Used by the ambiguous-name picker.
- `POST /api/set-prices {prices: [{name, price}]}` → bulk price update. Used by the "Preise eintragen" modal (manual fallback).
- `POST /api/firestore-sync?force=1` → fetches a fresh Firestore season-doc and re-syncs `players_available.json`. Returns `{ok, players_in_snapshot, snapshot_age_s, prices_changed, added, removed, pending}`. 401 with `hint` if `data/firebase_auth.json` is missing.

## Algorithms (in `app.js`)

Four are run on every "Team Optimieren" click; the comparison tab shows all side-by-side.

| Algorithm | Objective metric | Notes |
|---|---|---|
| **Optimal** (B&B) | `Σ avgPerTournament` | Raw season average. |
| **Konsistent** (B&B) | `Σ adjustedPT` (Bayes shrinkage, k=3) | Penalises low-tournament-count players. |
| **Turnier-Prognose** (B&B) | `Σ (avgPerMatch × expectedMatches)` | Only enabled when `tournament_sim.json` exists. |
| **Turnier-Manuell** (B&B) | `Σ (avgPerMatch × manualExpectedMatches)` | Only enabled when manual bracket overrides exist. |

Single solver: `optimizeBranchBound` (DFS + fractional-knapsack upper-bound pruning). Supports max-Block / max-Abwehr / size constraints.

**Captain feature:** Each team has one captain (auto-assigned to highest-value player) who scores 1.5×. The B&B objective is `sumVal + 0.5 × captainVal` where `captainVal = max(objective values in current team)` (the 0.5× is the bonus on top of the base 1×). Upper-bound pruning accounts for this via `extraCaptainBound = 0.5 × (globalMaxVal − captainVal)`.

**Picks & Bans** (`lockedPlayerIds`, `bannedPlayerIds`, persisted in `localStorage`):
- Locked players are pre-seeded into the team before `optimizeBranchBound` runs (budget/slots reduced accordingly).
- Banned players are filtered out of the candidate pool.
- Controlled by the "🔒 Meine Picks & Ausschlüsse verwenden" checkbox in the compare tab (`usePicks` flag). When unchecked, a pure-optimal run is shown instead.

## Win probability — two functions, intentionally different

In `simulate_tournament.py`. Both share the same 4-step decision logic with `_CLEAR = _CLOSE = 0.10`:

1. **Team H2H clear** (≥ 3 games, win-rate outside [0.40, 0.60]) → use team ratio
2. **Individual H2H clear** (≥ 3 total individual games, weighted avg outside [0.40, 0.60]) → use `aggregate_individual_h2h`
3. **DVV ratio** if |ratio − 0.5| > 10 % → use DVV
4. **DVV close** → use team H2H or individual H2H as tiebreaker; else coin flip

- **`win_prob`** (Monte-Carlo): returns exactly 0.5 when no clear signal (coin-flip).
- **`predict_prob`** (deterministic bracket display): returns `(prob, reason_str)`, never flattens to 0.5, shows raw ratio even for close games. Reason values: `"h2h"`, `"h2h_ind"`, `"dvv"`, `"seeding"`, `"fifty_fifty"`, `"no_data"`.

## Individual H2H (Einzel-Bilanzen)

The GBT H2H endpoint (`POST https://gbt.hanski.de/h2h/index.php?gender=m`) returns individual player stats in the same response as team stats — no extra HTTP calls needed.

- Parsed from `<details class="bilanz-item">` elements.
- Stored in H2H disk cache under `"individual": {"last_a|||last_b": {"w": int, "l": int}}`.
- Old cache entries without `"individual"` are backward-compatible (default to `{}`).
- `_flip_individual(ind)` reverses player order when swapping canonical team order.
- `aggregate_individual_h2h()` uses **weighted average** (total_wins / total_games) — a 10:6 pairing outweighs a 1:0 pairing.
- `indBreakdown` is written into each match in `bracketPrediction` for the modal display.

## Seeding-based DVV estimates (`augment_rankings_with_seedings`)

For international teams without DVV data, points are **linearly interpolated** between the nearest known seeds above and below. The global set `_synthetic_team_names` tracks which entries are synthetic so `lookup_team_points_traced` returns `source="seeding"` (amber badge in UI) instead of `source="team"` (green "DVV Team" badge).

## Team-points lookup (`lookup_team_points_traced`)

Three-level fallback. The trace is shown in the match-detail modal.

1. **`team`** — exact team-name hit in DVV team rankings (or `seeding` if synthetic).
2. **`individuals`** — sum of last-name hits in DVV individual rankings (`id=336/337`).
3. **`shares`** — half of best team per player as last resort.

## Pool estimates for zero-stat players

`computePoolEstimates(players)` in `app.js`: players with `tp=0 && t=0` get `avgPerTournament` estimated at 75 % of the weighted mean of same-position, ±5-coin peers. Shown with a `~geschätzt` badge. Applied only to `availablePlayers` (priced players), not `allPlayers`.

## DVV Tournament Scraper (`scripts/dvv_tournament.py`)

Primary source for bracket data. Scrapes `https://beach.volleyball-verband.de/public/`:

- **`discover_current_tournament(gender)`** — GET `tur.php?saison=<year>`, parses the tournament table, filters to category `Deutsche Beach-Volleyball Tour\German Beach Tour` and the right gender (`Männer`/`Frauen`), picks the earliest entry whose `date_end >= today`. State is `running` if today is in [start, end], else `upcoming`. Cache TTL 1 h under `data/.cache/dvv_tour_list_<year>.json`.
- **`fetch_setzliste(id)`** — GET `tur-sl.php?id=<id>`, returns `[{seed, players: [lastname, lastname], team_id, club, dvv_points}, ...]`. Cache TTL 1 h.
- **`fetch_spielplan(id)`** — GET `tur-sp.php?id=<id>`, returns `[{match_num, round, team_a, team_b, result: {winner: 'A'|'B'|None, sets, detail, points_a, points_b}}, ...]`. Empty list when the draw isn't out yet. Cache TTL **30 min** because results trickle in during play.
- **`build_bracket(gender)`** — composition that returns a dict in the legacy gbt.hanski.de schema:
  - `meta` includes `source='dvv'`, `name`, `tournamentId`, `gender`, `status` (`drawn`/`pending`/`running`/`complete`), `dateStart`, `dateEnd`.
  - `teams` keyed by seed string (`"1"..."8"`) with `{seeding, players, teamId, club, dvvPoints}`.
  - `rules` = `RULES_8_DOUBLE_ELIM` template (M1=S1vsS8, …, M13=W11vsW12) when there are exactly 8 numeric seeds; empty dict otherwise (downstream falls back to generic elim).
  - `matches` = raw Spielplan list (NEW; used by `simulate_gbt_bracket` and `compute_bracket_prediction` to **lock outcomes of already-played matches** instead of rolling dice).

**Wire-in**: `simulate_tournament.fetch_tournament_bracket(gender)` tries DVV first; on failure or non-8-team bracket, falls back to `_fetch_gbt_bracket_legacy()`. Output schema is intentionally identical so existing consumers don't change.

**Played-match override**: both `simulate_gbt_bracket` (Monte-Carlo) and `compute_bracket_prediction` (deterministic display) check `bracket.matches` and lock the winner when `result.winner` is set. Predicted-bracket display flags these matches with `reason='played'` (vs. `dvv`/`h2h`/etc.).

## Firestore Sync (`scripts/firestore_sync.py`)

Source of truth for **current-season prices and the active player list**. The gbt-fantasy.web.app project stores `season_stats/2026` in Firestore — one map field per player ID, with `pr` (price), `fn`/`ln`, `pos`, `g`, `tp`, `t`, `mp`, `ip`. Firestore REST requires auth (403 without).

**Auth model** — user runs `fetch_auth_token.txt` once on `https://gbt-fantasy.web.app/` after logging in. It writes `data/firebase_auth.json` (gitignored) containing the Firebase API key (public — not a secret) and the user's **refresh token** (sensitive). The server exchanges that for fresh 1-hour ID-tokens via `securetoken.googleapis.com/v1/token` whenever needed.

**Two-layer cache**:
- In-memory ID-token cache (~50 min) so a single sim run reuses one token.
- Disk-cache `data/.cache/firestore_season.json` (TTL **10 min**) so repeated UI clicks don't hit Firestore.

**Failure semantics** — if `firebase_auth.json` is missing, `fetch_firestore_season()` returns `None` silently and downstream falls back to the local-only flow. The explicit `/api/firestore-sync` endpoint returns **401 + hint** to drive the user to set things up.

**Wire-in points**:
- `sync_players_available_from_brackets` — Firestore `pr` overrides existing prices (logs diff as `prices_changed`).
- `map_teams_to_players` — when `fs_season` is provided, candidate pool per last-name is **narrowed** to player IDs present in the season doc. Falls back to the unfiltered pool per surname if narrowing would leave zero candidates (stale-snapshot guard). This is the primary fix for ambiguous surnames like the four-Wüst case.

## Mapping bracket teams → player IDs (`map_teams_to_players`)

Called once per sim run to build the `team → [playerId]` table that feeds `playerExpectedMatches`. When a bracket last name has multiple candidates in `players_all.json` (e.g. four Wüsts but only two play this tournament), the function:

1. Prefers a full name already in `players_available.json` (the user's confirmed pick from the ambiguous-name picker) that hasn't been assigned to another bracket slot yet.
2. Falls back to the highest-`tp` candidate not yet assigned.
3. Last resort: the highest-tp candidate overall (only when bracket has more slots for that surname than `players_all.json` has rows).

The per-surname `assigned` set guarantees two bracket slots with the same last name pick **different** full names. Before this was added, a naive last-name → ID dict overwrote on collisions, so Tamo and Lui Wüst both collapsed to whichever Wüst happened to come last in iteration order — and `expectedPoints` was `null` for the actual bracket players.

## Sync logic (`sync_players_available_from_brackets`)

Runs at the start of every simulation:
- Only numeric seedings 1–N are kept; `Q*` qualifiers and seed 99 withdrawals are skipped.
- New bracket players → `price: -1`. Existing prices are preserved.
- Players no longer in any bracket are **dropped**.
- Ambiguous last names: existing entry in `players_available.json` wins; otherwise highest-`tp` wins.
- Ambiguous matches → `tournament_sim.json → syncInfo.ambiguous` → frontend offers the name picker.

## Manual bracket overrides

- Toggle "✏ Manuell anpassen" in the Bracket tab enables click-to-set-winner per match.
- Overrides stored in `localStorage` under key `manualOverrides_{gender}_{tournamentId}`.
- `deriveManualBracket(basePrediction, overrides)` re-derives the full bracket deterministically from overrides, propagating W/L refs correctly.
- `computeManualExpectedMatches()` runs a deterministic walk over the derived bracket to produce expected matches per player → feeds the **Turnier-Manuell** algorithm.

## Frontend banner system

Two distinct mechanisms — keep them separate when adding new banners:

- **`showSimBanner`** (single element `#simBanner`) — transient toast: loading spinner / "✓ Aktualisiert" / errors. New calls overwrite.
- **`#syncWarnings` container** — persistent stack rebuilt by `showSyncWarnings`. Contains: pending prices, unknown names, missing-in-stamm-data, ambiguous-names. Ambiguous banner suppressed via `localStorage` once user saves picker (key invalidates if candidate set changes).

## Conventions worth following

- **Two genders mix in one Fantasy team** — never overwrite the other gender's block in `tournament_sim.json`. Read-merge-write pattern is in `_run`'s output section.
- **Cache busting**: every JSON `fetch()` appends `?t=Date.now()`.
- **All UI strings are German.** Match that style for new banners/labels.
- **Player IDs are strings** (`"2170783"`), not numbers — keep them as strings end-to-end.
- The Bracket tab uses an 8-row CSS grid with explicit `grid-row: start/end`. Don't use fractional `--row` values; they silently overlap.
- `predict_prob` returns a **tuple** `(float, str)` — callers must unpack it. `win_prob` returns a plain `float`.
- H2H disk cache keys for individual records use `"|||"` as separator (e.g. `"mueller|||ehlers"`); deserialize by splitting on `"|||"`.
