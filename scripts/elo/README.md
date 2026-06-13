# True ELO Rating System

Standalone ELO module for the German Beach Tour, intentionally decoupled from
the live Fantasy Optimizer pipeline. Lives in `scripts/elo/`, writes to
`data/elo_ratings.db` and `data/matches.csv`. Once the ratings are validated
we can wire them into `app.js` algorithms — until then this module is its own
island.

## Setup

```powershell
pip install -r scripts/requirements.txt
```

Uses only `requests`, `beautifulsoup4` and Python stdlib (`sqlite3`, `csv`,
`unittest`). Pandas is **not** required despite the original spec; CSV parsing
is fine with stdlib for this dataset.

## Run order — strict phased CLI

Every phase is opt-in. Re-running a phase touches the network only for
endpoints that aren't already in `data/raw/dvv/` (raw HTML cache, forever).

```powershell
# 1. List German Beach Tour tournaments for the seasons you care about
python scripts/elo/build_ratings.py --phase discover --saisons 25,26 --gender m

# 2. Spielpläne (Hauptfeld + Qualifikation) for every discovered tournament
python scripts/elo/build_ratings.py --phase tournaments

# 3. (optional) Match-detail pages. Defaults to unlimited, use --limit to dry-run
python scripts/elo/build_ratings.py --phase matches --limit 20
python scripts/elo/build_ratings.py --phase matches

# 4. Team pages — resolves firstname/lastname for every team_id
python scripts/elo/build_ratings.py --phase teams

# 5. (optional) FIVB / AVP archive (BigTimeStats)
python scripts/elo/build_ratings.py --phase fivb

# 6. Offline build — consolidates everything, runs ELO, writes SQLite DB
python scripts/elo/build_ratings.py --phase build
```

After `--phase build` the script prints the top-20 individuals, the backtest
accuracy for matches from 2025-01-01 onwards, and a calibration table.

## Predict a matchup

```powershell
python scripts/elo/predict.py --team1 Henning Pfretzschner --team2 Just Winter
```

Output names the players (handles ambiguous surnames with a warning), shows
individual + team + blended ELO, win-probabilities for both sides, and a
confidence band based on matches played.

## Politeness

DVV is a small federation site, not a CDN — three safeguards keep traffic
gentle:

1. **Persistent raw-HTML cache** under `data/raw/dvv/`. Each `(url, params)`
   pair is fetched once across the module's entire lifetime. Cached files are
   considered immutable for completed tournaments.
2. **Token-bucket throttle** in `scraper._throttle()`. Default is
   0.75 s ± 0.25 s jitter between live requests; override at runtime with
   `$env:ELO_SCRAPE_DELAY = "1.5"`.
3. **Phased CLI**. No single command fans out into thousands of requests
   without explicit opt-in. The `--phase matches --limit N` flag exists
   precisely so you can sniff-test the parser on a handful of pages before
   the full sweep.

The end of every run prints `HTTP requests / cache hits / errors` so you
always know what touched the network.

## Architecture

```
scripts/elo/
  scraper.py        DVV + FIVB I/O; throttle, retry, raw HTML cache
  elo.py            Pure ELO math (no I/O, no globals); fully unit-tested
  build_ratings.py  Phased orchestrator + SQLite writer
  predict.py        CLI to query the DB
  tests/test_elo.py 21 unit tests covering expected/update/mov/k/decay/blend
```

### Ratings tracked

Three rating types per identity:

- `elo_individual` — every player rated independently
- `elo_team` — every alphabetically-sorted partnership (`lastname_first|lastname_second`)
- `elo_combined` — the predictive blend used by `predict.py`:
  `0.6 · mean(elo_individual) + 0.4 · elo_team`, falling back to pure individual
  mean when the partnership has < 5 matches together.

### Parameters (in `elo.EloConfig`)

| Param | Default | Notes |
|---|---|---|
| `start` | 1500 | initial rating for new players/teams |
| `k_base` | 40 | base K-factor |
| `provisional_matches` | 20 | provisional period count |
| `provisional_multiplier` | 1.5 | K boost in provisional period |
| `importance_quali` | 0.75 | K multiplier for qualifying matches |
| `importance_main` | 1.0 | K multiplier for main draw |
| `importance_final` | 1.25 | K multiplier for finals / 3rd-place |
| `decay_pull` | 0.10 | seasonal pull strength toward 1500 |
| `decay_min_matches` | 3 | season-activity threshold |
| `team_min_matches_for_blend` | 5 | team-ELO blending threshold |
| `source_weight_fivb` | 0.5 | FIVB/AVP matches count half (different competition level) |

### Margin of victory

When set scores are present (always the case for DVV via
`point_summary = "21:18, 21:15"`), the MoV multiplier is
`1 + ln(1 + Δpoints/10)`, where `Δpoints` is the total point differential
across all sets. Falls back to discrete 1.2 (2:0) / 0.9 (2:1) when no set
scores were parsed.

### Match-by-match update order

1. Read pre-match ratings for both individuals on each team + the two teams
2. Compute each team's `blended` rating (individual + team mixed)
3. Update each player's individual ELO against the **opposing team's blended**
   rating (team context informs individuals without double-counting)
4. Update both team-ELOs against each other directly
5. Persist an `elo_history` row for every entity touched

### Seasonal decay

Applied at the boundary between DVV seasons (`saison` field on the discovered
tournament). For every individual who played ≥ 3 matches in the season that
just ended:

```
new_rating = old + 0.10 * (1500 - old)
```

Players who didn't reach the activity threshold are left alone (no decay
penalty for one-tournament wonders).

## Known limitations (Phase-1 scope)

1. **Name merging across DVV ↔ FIVB**: DVV uses German nicknames ("Max Just"),
   FIVB uses passport names ("Maximilian Just"). The current ID
   (`lastname_firstname`) does not merge these — they appear as two entities.
   A name-alias map would fix this in Phase 2.
2. **Coverage**: Phase-1 default is DVV men's 2025+2026 only. Adding women or
   earlier seasons just means more `--phase discover --saisons N,N+1`.
3. **Backtest with cold start**: every player begins at 1500, so early matches
   in 2025 dilute backtest accuracy. The FIVB overlay improves this for
   internationally-active players but not for pure DVV regulars.
4. **No incremental rebuild**: every `--phase build` drops and re-creates the
   DB. Cheap thanks to the raw HTML cache.

## Tests

```powershell
cd scripts && python -m unittest discover -s elo/tests
```

21 unit tests; runs in well under a second.
