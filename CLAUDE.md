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
python scripts/serve.py            # serves on :8000  (or $PORT)
python scripts/serve.py 8123       # explicit port

# Run the simulator manually (also runs implicitly via the server's /api/simulate)
python scripts/simulate_tournament.py --gender m
python scripts/simulate_tournament.py --gender f
python scripts/simulate_tournament.py --gender m --simulations 50000 --force-refresh

# Fetch a fresh Firestore snapshot from the CLI (handy for debugging auth)
python scripts/firestore_sync.py --print

# Probe what DVV thinks today's tournament is
python scripts/dvv_tournament.py --gender m --print
```

**Always use `serve.py`, not `python -m http.server`.** The static server has no API endpoints — clicking "Prognose neu" or the price/ambiguity pickers will return 404.

After Python code changes you must restart `serve.py` (Ctrl+C → re-run); Python doesn't hot-reload. After frontend changes the browser also needs a hard reload (Ctrl+Shift+R) — `serve.py` already sends `Cache-Control: no-store` for `.js`/`.html`/`.css`/`.json`, so a normal reload works after the first hard reload.

There are no tests, no linter, no build, no package.json scripts.

### One-time setup for Firestore-backed features

To get auto-synced prices, ambiguous-name resolution and Firestore-only players (rookies), the server needs a Firebase refresh token:

1. Log in at `https://gbt-fantasy.web.app/`.
2. Open DevTools → Console.
3. Paste the contents of `fetch_auth_token.txt`. It downloads a `firebase_auth.json`.
4. Either put the file at `data/firebase_auth.json` **or** copy its `apiKey`/`refreshToken` values into `.env.local` (see `.env.local.example`).
5. Restart `serve.py`.

Without this the server still works: `firestore_sync` soft-fails to `None`, the manual "Preise eintragen" picker stays available, and you lose only the auto-roster + auto-price features.

### Environment variables

`scripts/_env.py` loads `.env` and `.env.local` (the latter wins) into `os.environ` at process start. Real env-vars from the shell / Docker / Fly secrets ALWAYS win — files only fill in what isn't already set.

| Var | Default | Purpose |
|---|---|---|
| `FIREBASE_API_KEY` | (from `data/firebase_auth.json`) | Firebase web API key (public). |
| `FIREBASE_REFRESH_TOKEN` | (from `data/firebase_auth.json`) | Long-lived refresh token (sensitive). |
| `DATA_DIR` | `<repo>/data` | Where all writable state lives. For Fly/Docker mount a volume here. |
| `PORT` | `8000` | `serve.py` port. Fly.io sets this to 8080. |
| `CURRENT_SEASON_YEAR` | system clock year | Override for backtesting or mid-year season transitions. |

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

Tracked in the repo:

| Path | Purpose |
|---|---|
| `index.html` | Four tabs: 📊 Alle Spieler · 🔒 Meine Picks · ⚖ Vergleich · 🏆 Turnier-Baum. The filter bar (`.player-filters` — pos/gender/status/price-range/sort) is injected by JS into BOTH the Alle-Spieler and Meine-Picks tabs from a shared `playerFilters` global; changes mirror across both. A top-level `🆚 H2H Vergleich` button opens an ad-hoc two-player comparator. |
| `app.js` | Single-file frontend. State held in module-level `let`. Renders into existing tab-content divs. |
| `styles.css` | Hand-written, no preprocessor. Dark theme with CSS custom properties under `:root`. |
| `scripts/_env.py` | Tiny stdlib-only dotenv loader + `data_dir()` resolver. Imported by every entry-point script. |
| `scripts/simulate_tournament.py` | Scraping, caching, sync, Monte-Carlo, deterministic prediction, JSON output. |
| `scripts/serve.py` | `ThreadingTCPServer` subclass. Imports `simulate_tournament` to handle the simulate endpoint inline. Creates empty `players_available.json` stub at startup so a fresh clone doesn't 404. |
| `scripts/firestore_sync.py` | Fetches gbt-fantasy.web.app Firestore docs via refresh-token auth — current season + every year from `EARLIEST_SEASON_YEAR=2025` up to today's year. Source of truth for prices, current roster, and historical season stats. |
| `scripts/dvv_tournament.py` | **Primary bracket source.** Scrapes `beach.volleyball-verband.de/public/`: discovers the next/current "Deutsche Beach-Volleyball Tour\\German Beach Tour" tournament per gender, parses Setzliste + Spielplan, and emits a bracket-dict in the legacy gbt.hanski.de schema (plus `meta.source='dvv'` and `matches[]` for already-played games). `fetch_tournament_bracket()` tries this first; on failure or non-8-team bracket, falls back to `_fetch_gbt_bracket_legacy()`. |
| `fetch_auth_token.txt` | Browser snippet to produce `firebase_auth.json` (run once on `https://gbt-fantasy.web.app/` after login). |
| `fetch_new_data.txt` | Legacy browser snippet to dump the current-season Firestore doc manually. Mostly obsolete now (`firestore_sync.py` does it automatically), kept as a backup. |
| `.env.local.example` | Template for `.env.local` (which is gitignored — see Environment variables above). |

Gitignored (auto-generated under `$DATA_DIR`):

| Path | Purpose |
|---|---|
| `data/firebase_auth.json` | Refresh-token file (alternative to `.env.local`). |
| `data/players_season_<year>.json` | Raw Firestore season-doc per year (`2025`, `2026`, …). Auto-fetched by `firestore_sync`; **one-shot** per year (historical data doesn't change). |
| `data/players_season.json` | Legacy alias for the current year's overlay — `firestore_sync` keeps it in sync for backward-compat. |
| `data/players_available.json` | List of `{name, price}` rebuilt from the current bracket on every sim run. Created as empty `[]` stub by `serve.py` on first start. |
| `data/tournament_sim.json` | Sim output. Keyed by `byGender.m` and `byGender.f`; `playerExpectedMatches` at top level is the merged across genders. |
| `data/.cache/` | Disk caches: DVV scrapes (1 h), gbt.hanski.de bracket fallback (1 h), H2H per-pair (24 h), Firestore snapshot (10 min). Wipe or pass `--force-refresh` to bypass. |

## Backend API (`scripts/serve.py`)

- `GET /api/sim-status` → `{exists, fresh, age_s, playersHashMatch, tournaments}`. "Fresh" = players-list hash matches AND age < 6 h.
- `POST /api/simulate?gender=m|f|all&force=1&qualifiers=1` → blocks until done, single-flight via `_sim_lock`. Returns `{ok, duration_s, status}`.
- `POST /api/swap-player {from, to}` → renames an entry in `players_available.json` (preserves price). If `to` already exists in the list (two bracket slots share a last name and the user wants to switch which full name goes where), performs a **true swap** instead of a rename — both `players_available.json` and `syncInfo.ambiguous[*].chosen` are swapped so the other slot keeps its own identity. Used by the ambiguous-name picker.
- `POST /api/set-prices {prices: [{name, price}]}` → bulk price update. Used by the "Preise eintragen" modal (manual fallback).
- `POST /api/firestore-sync?force=1` → fetches a fresh Firestore season-doc and re-syncs `players_available.json`. Returns `{ok, players_in_snapshot, snapshot_age_s, prices_changed, added, removed, pending}`. 401 with `hint` if `data/firebase_auth.json` is missing.

## Algorithms (in `app.js`)

Up to five algorithms run on every "Team Optimieren" click — the comparison tab shows all available ones side-by-side.

| Algorithm | Objective metric | Enabled when |
|---|---|---|
| **Optimal** (B&B) | `Σ avgPerTournament` | always |
| **Konsistent** (B&B) | `Σ adjustedPT` (Bayes shrinkage, k=3) | always |
| **Turnier-Prognose** (B&B) | `Σ (avgPerMatch × expectedMatches)` | `tournament_sim.json` exists |
| **Turnier-Manuell** (B&B) | `Σ (avgPerMatch × manualExpectedMatches)` | manual bracket overrides exist |
| **Finale-Fokus** (B&B) | `Σ finalRoundObjective` (`roundLevel × 1000 + avgPerTournament` — primary axis = reaching semi (1000) / final (2000), tiebreaker = season avg) | `tournament_sim.json` exists |

`getObjectiveValue(player, alg)` is the single switch keying each algorithm to the player attribute it maximizes.

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

Source of truth for **prices, current roster, and historical season stats**. The gbt-fantasy.web.app project stores `season_stats/<year>` docs in Firestore — one map field per player ID, with `pr` (price; only on current year), `fn`/`ln`, `pos`, `g`, `tp`, `t`, `mp`, `ip`. Firestore REST requires auth (403 without).

**Auth model** — user supplies a Firebase **refresh token** (long-lived) plus the public **API key**, either:
- via `.env.local` / real env-vars: `FIREBASE_API_KEY`, `FIREBASE_REFRESH_TOKEN`  *(preferred — required for Docker/Fly deploys)*, or
- via the legacy `data/firebase_auth.json` file (still supported, file-fallback in `_load_auth()`).

The server exchanges the refresh token for 1-hour ID-tokens via `securetoken.googleapis.com/v1/token` on demand. ID-tokens are cached in-memory for ~50 min so a single sim run reuses one.

**Multi-year fetch model** — `season_years()` returns `[EARLIEST_SEASON_YEAR … current_season_year()]`. `EARLIEST_SEASON_YEAR=2025` was empirically determined (Firestore returns 404 for older). On every `fetch_firestore_season()` call:

1. The current year is fetched (10-min disk cache TTL, write both `players_season_<year>.json` and the legacy `players_season.json`).
2. Each archive year is fetched **once** via `fetch_archive_season(year)` — file-exists check skips re-fetching since historical seasons don't change.
3. When the system clock rolls over to a new year, the new year is auto-attempted on next sync (no code change needed at season-rollover).

`current_season_year()` is `os.environ["CURRENT_SEASON_YEAR"]` if set, else `datetime.date.today().year`. Useful for backtesting.

**Failure semantics** — if auth is missing, `fetch_firestore_season()` returns `None` silently and the rest of the pipeline degrades gracefully (manual price picker still works, ambiguity picker re-appears). The explicit `/api/firestore-sync` endpoint returns **401 + hint** to drive the user to set things up.

**Wire-in points**:
- `sync_players_available_from_brackets` — Firestore `pr` overwrites stored prices (logs each diff as `prices_changed`). Also runs even when both brackets are empty (between tournaments) to refresh prices without wiping the player list.
- `map_teams_to_players` — when `fs_season` is provided, the per-last-name candidate pool is **narrowed** to player IDs present in the current season doc. Defensive: falls back to the unfiltered pool per surname if narrowing would leave zero candidates (stale-snapshot guard).
- Both `map_teams_to_players` and `sync_players_available_from_brackets` **synthesize player records from Firestore data** for IDs not in `players_all.json` — that's how rookies like Milan Sievers (Firestore-only) get resolved.

## Multi-year roster (frontend overlay merge)

Since `players_all.json` was removed from the repo, the **frontend builds the player roster from the Firestore overlays alone**:

- `loadAllSeasonOverlays()` in `app.js` probes `data/players_season_<year>.json` for every year in `2025..(currentYear+1)`, 404s on missing years are silently skipped, falls back to the legacy `players_season.json` if no year-suffixed file exists.
- `loadPlayerData()` then builds `allPlayers` by:
  - **Roster** = union of all overlays' player IDs.
  - **Identity** (`firstName`, `lastName`, `pos`, `gender`, `img`) = newest year overlay that has `fn`/`ln` for the id. `players_all.json` is read only as an identity fallback for IDs no overlay covers.
  - **Stats** (`tp`, `t`, `mp`) = **summed** across all overlay years. The old "players_all + current overlay" addition was retired — it accidentally worked because `players_all.json` happened to equal the 2025 export, but would have double-counted as soon as more years existed.

If you ever need pre-2025 historical stats, that data simply isn't in Firestore — `EARLIEST_SEASON_YEAR` would have to be lowered AND someone would have to populate those docs.

## Mapping bracket teams → player IDs (`map_teams_to_players`)

Called once per sim run to build the `team → [playerId]` table that feeds `playerExpectedMatches`. The candidate pool is `players_all.json` (if present) PLUS every Firestore-only player synthesized from `fs_season` — so rookies missing from `players_all` still resolve. When a bracket last name has multiple candidates (e.g. four Wüsts but only two play this tournament), the resolution layers are:

1. **Firestore narrowing**: if `fs_season` is provided, the candidate pool per surname is restricted to player IDs in the current season doc. Falls back to the unrestricted pool per surname if narrowing yields zero candidates (stale-snapshot guard).
2. **User confirmation**: a full name already in `players_available.json` (i.e. the user picked it via the ambiguous-name modal) wins.
3. **Highest-tp candidate** that hasn't been assigned to another slot yet.
4. **Last resort**: the highest-tp candidate overall (only when the bracket has more slots for that surname than there are candidate rows).

The per-surname `assigned` set guarantees two bracket slots with the same surname pick **different** full names. Before this was added, a naive last-name → ID dict overwrote on collisions, so Tamo and Lui Wüst both collapsed to whichever Wüst happened to come last in iteration order — and `expectedPoints` was `null` for the actual bracket players.

## Sync logic (`sync_players_available_from_brackets`)

Runs at the start of every simulation. Order of operations:

1. **Try Firestore** (`fetch_firestore_season(force=force)`) — soft-fails to `None` if no auth.
2. **Augment the player pool** with Firestore-only players (rookies absent from `players_all.json`).
3. **Walk both gender brackets** (DVV primary, gbt.hanski.de fallback). Numeric seedings 1..N are kept; `Q*` qualifiers and seed 99 withdrawals are skipped.
4. **Two safety modes** for the write-back:
   - **Both brackets empty** (between tournaments): refuses to wipe `players_available.json`. Instead loops through existing entries and updates **only prices** from the Firestore snapshot, then returns.
   - **Brackets populated**: rebuilds the list to match the bracket roster. New bracket players land with `price` from Firestore (or `-1` fallback). Players no longer in any bracket are dropped. Existing user prices are overwritten when Firestore has a different value (each diff logged into `prices_changed`).
5. **Ambiguous matches** (surnames with multiple plausible candidates after Firestore narrowing) → recorded into `tournament_sim.json → syncInfo.ambiguous` → frontend offers the picker UI.

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
- **`data/` writes must go through `data_dir()`** (in `scripts/_env.py`), never hardcoded `ROOT / "data"` — the Fly-deploy path relies on `$DATA_DIR` pointing at a mounted volume.
- Don't commit anything under `data/`. The `.gitignore` whitelists nothing — the directory is meant to be machine-local state.

## Repo & branches

- `main` — primary working branch.
- `fly-deploy` — held in sync with `main` and used for Fly.io-specific commits (Dockerfile, `fly.toml`, etc.) once those land. Anything not Docker/Fly-specific belongs on `main`.
- History has been rewritten with `git filter-repo` to scrub stale `data/*.json` snapshots. If you ever need to do another scrub, the executable is at `~/AppData/Roaming/Python/Python311/Scripts/git-filter-repo.exe` (Windows install via `pip install --user git-filter-repo`).
