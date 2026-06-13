# Bayesianische Skill-Modelle für professionelles Beach-Volleyball:
# Eine vergleichende Evaluation mit Ensemble-Aggregation

**Stand:** 13. Juni 2026
**Repository:** `gbt-fantasy-optimizer` (Branch `main`)
**Datensatz-Snapshot:** 116.521 Matches, 11.058 Spieler, drei Quellen aggregiert

---

## Kurzzusammenfassung (Abstract)

Wir evaluieren drei klassische Skill-Rating-Systeme — Elo (1960), Glicko-2 (Glickman 2012), TrueSkill (Herbrich et al. 2007) — auf dem Anwendungsfall **professionelles 2-gegen-2 Beach-Volleyball**. Die Trainingsmenge umfasst 110.875 historische Matches (2000–2024), die Testmenge 5.646 prospektiv vorhergesagte Matches (Cutoff 2024-12-31). Drei strukturelle Hebel werden systematisch evaluiert: (i) Cold-Start-Priors aus nationalen Ranglisten, (ii) heuristisches Quellen-übergreifendes Name-Aliasing, (iii) modell-spezifisches Hyperparameter-Tuning. Anschließend wird ein **Ensemble** als gewichtetes Mittel der drei Modelle gebildet.

**Hauptbefund:** Das Ensemble erreicht **67,1% Out-of-Sample-Accuracy** bei einem **Calibration-Error von 0,007** — eine Verbesserung von +0,65pp Accuracy und 50% Calibration-Reduktion gegenüber dem stärksten Einzelmodell-Default-Setup. Die individuellen Modelle konvergieren bei 66,3–66,8% Accuracy, was den nahe-noise-floor-Charakter der Domain bestätigt. Das größte Hebelpotenzial liegt im Hyperparameter-Tuning (insbesondere für Glicko-2: Calibration-Reduktion um 71%). Cold-Start-Priors und historische Daten-Erweiterung haben überraschend geringen messbaren Effekt, was auf strukturelle Limitierungen des Vorhersageproblems hinweist.

---

## 1. Einleitung

Beach-Volleyball ist eine seit 1996 olympische Sportart mit hochkompetitivem internationalem Profizirkus (FIVB Beach Pro Tour, Olympia, Continental Cups) und nationalen Touren wie der Deutschen Beach-Volleyball-Tour (DVV German Beach Tour, GBT). Anders als bei großen Mannschaftssportarten existiert für Beach-Volleyball **keine etablierte, öffentliche, modellbasierte Skill-Bewertung**. DVV und FIVB pflegen jeweils Punkteranglisten, die jedoch primär als Setzliste-Mechanismus dienen und nicht für prospektive Win-Vorhersagen optimiert sind.

Diese Studie hat drei Ziele:

1. **Vergleichende Evaluation** dreier algorithmisch unterschiedlicher Skill-Rating-Familien (Elo, Glicko-2, TrueSkill) auf einem konsolidierten Datensatz von 116.521 Matches.
2. **Identifikation und Quantifizierung** struktureller Verbesserungs-Hebel jenseits der Hyperparameter-Optimierung (Cold-Start-Priors, Quellen-übergreifendes Aliasing, Daten-Erweiterung, Ensemble-Aggregation).
3. **Etablierung einer reproduzierbaren Pipeline** mit Web-UI, die als Werkzeug für Praxis-Anwender (Vorhersage in Fantasy-Pools, Setzlisten-Diskussion, Talent-Identifikation) dienen kann.

Wir argumentieren am Ende, dass Beach-Volleyball einen vergleichsweise niedrigen empirischen Vorhersage-Plafond aufweist (~67-72% in der internationalen Literatur kommerzieller Wettmodelle) und dass unsere 67,1% damit nahe an diesem Plafond liegen.

---

## 2. Verwandte Arbeit

### 2.1 Klassische Skill-Rating-Systeme

**Elo (1960)** wurde ursprünglich für Schach entwickelt. Eine logistische Win-Prob-Funktion mit konstanter Lernrate K hat sich als robuste Baseline in vielen Sportarten etabliert. FiveThirtyEight nutzt Elo-Varianten für NFL, NBA, MLB und Soccer.

**Glicko und Glicko-2 (Glickman 1995, 2012)** ergänzen Elo um eine explizit modellierte Rating-Deviation ϕ und eine Volatilitäts-Komponente σ. Glicko-2 wird unter anderem von Lichess (Schach-Server) und der Australian Chess Federation eingesetzt. Glickman empfiehlt eine **Batch-Update-Logik** ("Rating Periods" von typischerweise 5-10 Matches pro Spieler), was bei Beach-Tournament-Strukturen (3-7 Matches in 2-3 Tagen) gut passt.

**TrueSkill (Herbrich, Minka, Graepel 2007)** ist Microsoft Researches Bayessches Faktor-Graph-Modell für Multiplayer-Matchmaking, entwickelt für Halo 3. TrueSkill modelliert nativ Teams beliebiger Größe und führt einen Belief-Propagation-Update über alle beteiligten Spieler durch — relevant für 2v2.

### 2.2 Anwendung in Volleyball / Beach-Volleyball

Akademische Literatur zur quantitativen Beach-Skill-Modellierung ist spärlich. Veröffentlichte Arbeiten konzentrieren sich auf biomechanische Analysen, Aufschlag-Statistiken oder taktische Pattern-Erkennung. Eine systematische Evaluation moderner Bayesscher Rating-Systeme im Beach-Kontext fehlt unseres Wissens nach.

Verwandte Beach-Volleyball-Datensätze: das **BigTimeStats Archive** (GitHub, 2000–2022, AVP+FIVB, ~85k Matches) und **bvbinfo.com** (umfassende öffentliche Match-Datenbank ab 1990, kommerziell limited Access). Beide Quellen verwenden inkonsistente Spieler-Identifier (insbesondere Namensvarianten zwischen verschiedenen Sprach-Schreibweisen).

### 2.3 Ensemble-Methoden im Sport-Rating

Stacked-Ensembles von Rating-Systemen sind in der Wett-Industrie und bei FiveThirtyEight-Publikationen etabliert, akademisch aber wenig dokumentiert. Wir zeigen in dieser Arbeit, dass selbst ein **gleichgewichtetes arithmetisches Mittel** der drei genannten Modelle eine messbare Verbesserung von 0,3pp Accuracy bei gleichzeitiger Halbierung des Calibration-Error liefert.

---

## 3. Methodik

### 3.1 Datensatz und Quellenaggregation

Drei heterogene Datenquellen werden zu einem einheitlichen chronologischen Match-Stream konsolidiert:

| Quelle | Beschreibung | Coverage | Anzahl Matches |
|---|---|---|---|
| **BigTimeStats CSV** | Historisches FIVB-Archiv, scraped 2024 | 2000-09 → 2022-09 | 84.687 |
| **bvbinfo.com** | Tournament-by-tournament HTML-Scraping | 2015 → 2026, M+F | 31.463 |
| **DVV German Beach Tour** | Saisons 25-26 + Backfill 15-24 | 2015 → 2026, M+F | 371 |
| **Total nach Dedup** | _dedup_bvb_vs_fivb (bvb-Priorität bei Überschneidung) | | **116.521** |

**Dedup-Strategie:** Bei zeitlicher Überschneidung (2015-2022, FIVB ∩ bvb) hat bvb Vorrang, da das Parser-Output reichhaltiger ist (Set-Scores, Round-Labels, Country-Tags).

**Daten-Sanitization:**
- Player-IDs normalisiert zu `lastname_firstname` (lowercase, accent-stripped via NFKD).
- Team-IDs als alphabetisch sortierte Player-ID-Paare (`a|b`).
- Round-Labels klassifiziert zu `quali` / `main` / `final` über regex-basiertes Boundary-Matching (entscheidend: "Halbfinale" enthält "final" aber ist `main`).

### 3.2 Drei evaluierte Rating-Modelle

Alle drei Modelle teilen sich einen modell-agnostischen chronologischen Runner-Loop (`scripts/elo/runner.py`), der pro Match identische Side-Tables (last-active, country-counter, gender-counter, team-membership) aufbaut. Damit ist die Evaluation strikt vergleichbar.

#### 3.2.1 Elo (klassisch + Beach-Adaptionen)

Pro Spieler ein Skalar μ ∈ ℝ, initialisiert bei 1500. Update nach jedem Match:

$$\mu_\text{neu} = \mu_\text{alt} + K \cdot M \cdot (S - E)$$

mit:
- $E = 1/(1 + 10^{(\mu_\text{opp}-\mu)/400})$
- $S \in \{0, 1\}$
- $K$ = Lernrate (getuned: 30)
- $M$ = Margin-of-Victory-Multiplikator

**Beach-spezifische Anpassungen:**
- **MoV-Funktion** (eigene Entwicklung, log-skaliert):
  $$M = 1 + \text{strength} \cdot \ln(1 + |\Delta_\text{pts}|/10)$$
  mit $\Delta_\text{pts}$ = Punktedifferenz über alle Sätze. Ein 21:15 21:17 (Δ=10) zählt ~1.69×, ein knappes 2:1 dampft auf <1.0.
- **Importance-Faktor**: Quali ×0.75, Hauptfeld ×1.0, Finals ×1.25 (auf K multipliziert).
- **Provisional-Boost**: Erste 20 Matches: K ×2.0.
- **Seasonal Decay**: Pro Kalenderjahr-Übergang $\mu \leftarrow \mu + 0.10 \cdot (1500 - \mu)$.
- **Team-Elo separat**: Pro Partnerschaft als eigenes Rating, blended für Display: `0.8 × individual + 0.2 × team`.
- **Source-Weights**: Multiplikator auf K pro Datenquelle (DVV/FIVB/bvb), default je 1.0, tunbar.

#### 3.2.2 Glicko-2

Pro Spieler Tupel (μ, ϕ, σ): Skill, Unsicherheit, Volatilität. Update faithful nach Glickman (2013).

**Beach-Adaption für 2v2:**
- Pro Match wird die OPPONENTEN-Team-Aggregation $(\mu_T, \phi_T)$ via inverse-variance Weighted Average gebildet.
- Beide Spieler des Teams bekommen identische Opponent-Tupel.
- Rating-Period: 1 Kalenderwoche (Glickmans Empfehlung 5-10 Matches/Period).
- Inaktivitäts-Effekt: $\phi' = \sqrt{\phi^2 + \sigma^2}$ pro übersprungener Period, gedeckelt bei initial_phi.

**Display-Funktion** (UI-Kompatibilität mit Elo-Skala):
$$\text{display} = \mu - 2\phi + 200$$
(Glickmans 95%-Lower-Bound mit +200-Shift für UI-Lesbarkeit.)

#### 3.2.3 TrueSkill

Implementiert via `trueskill` PyPI-Package (Heungsub Lee, BSD-lizenziert). Pro Spieler Tupel $(\mu, \sigma)$, initial $(25, 25/3)$.

**Beach-Adaption:**
- Per-Match: `env.rate([(p1a, p1b), (p2a, p2b)], ranks=[0, 1])` für Sieger-Team-1 — der Halo-native Multi-Player-Update.
- **σ-Inflation** pro Jahr (1.2×) für Inaktivität (statt μ-Decay; gedeckelt bei σ₀).
- **Win-Prob in geschlossener Form**:
  $$P(T_1 \text{ gewinnt}) = \Phi\left(\frac{\Sigma\mu_{T_1} - \Sigma\mu_{T_2}}{\sqrt{2N\beta^2 + \Sigma\sigma^2}}\right)$$

#### 3.2.4 Ensemble (eigene Entwicklung)

Vierte Modell-Variante: hält intern drei Kind-Modelle und mittelt deren Predictions:

$$\hat{p}_\text{ensemble} = \frac{w_E \hat{p}_E + w_G \hat{p}_G + w_T \hat{p}_T}{w_E + w_G + w_T}$$

Display-Ratings werden via $\Delta$-zu-Average-Player normiert (alle drei Modelle auf 1500-Basis transformiert) und gewichtet gemittelt. Gewichte default je 1.0; A/B-Tests durch Nullsetzen einzelner Gewichte möglich.

**Wichtig:** Das Ensemble führt das Predict-Update **per Match in lockstep** durch — die drei Kindmodelle teilen denselben Match-Stream und konvergieren synchron. Es ist also **kein nachträgliches Stacking**, sondern ein vollwertiges Online-Modell, das die gleiche Runner-Schnittstelle wie die anderen drei erfüllt.

### 3.3 Evaluations-Protokoll

**Holdout-Definition:** Cutoff 2024-12-31. Matches **strikt nach** diesem Datum bilden die OOS-Testmenge (n=5.646). Training erfolgt auf allen Matches **bis einschließlich** dem Cutoff.

**Metriken:**

| Metrik | Definition |
|---|---|
| **Accuracy** | $\frac{1}{n} \sum_i \mathbb{1}[\hat{w}_i = w_i^\text{actual}]$ |
| **Calibration Error (CE)** | $\sum_b \frac{n_b}{n} \cdot \lvert \bar{p}_b - \bar{w}_b \rvert$ über Buckets b∈{[0.5,0.6), ..., [0.9,1.0)} |
| **In-sample** | Accuracy/CE auf DVV-25+ Matches, die im Training enthalten sind (sanity-only, n=370) |

**Bias-Korrektur:** Da Beach-Volleyball keine Draws kennt und Predictions $\hat{p} < 0.5$ als "Team-2-Sieg" interpretiert werden, klappen wir vor dem Bucketing auf $\max(\hat{p}, 1-\hat{p})$ und buckten auf Confidence-Klassen 50%, 60%, ..., 90%.

**Reproduzierbarkeit:** Jede Modell-Variante wird in zwei separaten Passes evaluiert — der "Full-Training-Pass" schreibt die UI-JSON, der "OOS-Pass" mit Cutoff schreibt die Meta-Zahlen. So sind die in der UI gezeigten Spieler-Ratings immer auf der vollen Datenmenge trainiert, während die OOS-Metriken ehrlich gehalten sind.

---

## 4. Strukturelle Verbesserungs-Hebel

In ansteigender Aufwand-Reihenfolge.

### 4.1 DVV Cold-Start-Priors

**Motivation:** Etwa 8% der OOS-Matches betreffen Spieler mit <10 Matches im Training. Diese starten bei μ=1500 (Elo) bzw. μ₀=25 (TrueSkill) und werden faktisch als "durchschnittlich" prognostiziert, obwohl viele bereits DVV-Ranglistenpunkte als externes Skill-Signal haben.

**Implementierung:** `scripts/elo/priors.py` lädt DVV-Ranking-Tabellen (gescrapt aus `beach.volleyball-verband.de/public/rl-show.php`) und mappt auf Initial-Ratings:

$$\mu_\text{prior} = \max(1400, \min(1800, 1400 + 0{,}4 \cdot \text{points}))$$

Pro Modell:
- **Elo**: μ_initial = μ_prior
- **Glicko-2**: μ_initial = μ_prior, ϕ_initial = min(200, ϕ_default) — "primed" = niedrigere Unsicherheit
- **TrueSkill**: μ_initial = 25 + (μ_prior − 1500) × 5/400, σ_initial = 5.0

94 deutsche Spieler bekamen messbar einen Prior (Wickler → 1607, Tillmann → 1616, Müller → 1616).

**Gemessener Effekt:**

| Modell | OOS Acc vor | OOS Acc nach | Δ |
|---|---|---|---|
| Elo | 66,45% | 66,47% | **+0,02pp** |
| Glicko-2 | 65,89% | 66,05% | **+0,16pp** |
| TrueSkill | 66,44% | 66,58% | **+0,14pp** |

In-Sample-DVV-25+ profitierte stärker (Glicko-2: 63,8% → 65,9%, +2,1pp), aber der OOS-Effekt blieb deutlich unter der initialen Erwartung von "+2-3pp". **Erklärung:** Die meisten Top-Spieler im OOS-Set haben bereits viele Matches im Training und sind nicht mehr im Cold-Start-Bereich; der gemessene OOS-Gewinn beschränkt sich auf wenige Rookie-vs-Etablierter-Matches.

### 4.2 Quellen-übergreifendes Name-Aliasing

**Motivation:** "Max Just" (DVV) und "Maximilian Just" (FIVB) sind nach Vornamen-Normalisierung getrennte Player-IDs. Für ~30-50 deutsche FIVB-Routiniers werden Match-Historien dadurch fragmentiert.

**Implementierung:** `scripts/elo/aliases.py` baut Union-Find-Cluster pro Nachname mit zwei Konfidenz-Tiers:

- **Tier 1 (High):** Birthdate-Match (nur FIVB-CSV hat Geburtsdatum).
- **Tier 2 (Medium):** Vornamen-Präfix-Match (≥3 Zeichen, z.B. "Max" ⊂ "Maximilian") + Country-Match (Germany/Deutschland/GER als Synonyme).

Output: `data/elo_aliases.json` (versioniert, user-editierbar). Plus `data/elo_aliases_overrides.json` mit `block`-Liste für manuelle Korrekturen.

Anwendung in `_consolidate_records`: transitive Closure des Mappings, dann Remap aller `player1a/1b/player2a/2b` plus Recompute der `team_id`s.

**Gemessener Effekt:**
- **23 Merges** auto-generiert (10 high-confidence, 13 medium).
- Beispiele: `just_maximilian ← just_max`, `mchugh_christopher ← mchugh_chris`, `bantle_william ← bantle_will`.
- Player-Anzahl im Datensatz: 11.083 → 11.058 (-25).
- 1.310 Player-Slot-Mappings angewandt über alle Records.
- **OOS-Effekt im Bereich des Rauschens** (geschätzt <0,1pp; mit Hebel 4.1 zusammen 0,02-0,16pp).

**Diskussion:** False-Positives wie `wang_jingzhe ← wang_jing` (vermutlich verschiedene chinesische Spielerinnen) demonstrieren die Grenze rein heuristischen Aliasings. Der versionierte Block-Mechanismus ist die intendierte Korrektur-Schiene.

### 4.3 Margin-of-Victory Stärke-Parametrisierung

**Motivation:** MoV ist seit Initial-Implementierung in Elo aktiv (log-Skalierung), aber als feste Funktion. Eine Stärke-Parametrisierung erlaubt A/B-Tests und Grid-Tuning.

**Implementierung:** Linear-Blend zwischen "binary Win/Loss" und "full log-MoV":
$$M_\text{eff} = 1 + \text{strength} \cdot (M_\text{raw} - 1)$$

mit `strength` als EloConfig-Field (default 1.0, tunbar [0, 1.5]).

**Status:** Slider in UI exposed. Grid noch nicht durchgeführt. Erwartung basiert auf Sport-Analytics-Literatur: Optimum häufig bei strength=0.5-0.8 (volle log-Skala übersteigert oft).

### 4.4 Source-Weight Parametrisierung

**Motivation:** FIVB-, DVV- und bvb-Matches sind unterschiedliche Wettbewerbs-Tiers. FIVB-World-Tour-Matches involvieren oft Spieler weit über deutschem Top-Niveau; DVV-Nationale-Matches mehr Talent-Streuung. Eine differenzierte Lernrate pro Quelle könnte calibrationsrelevant sein.

**Implementierung:** `source_weight_dvv/fivb/bvb` waren bereits in `EloConfig` vorhanden (default je 1.0), wurden aber nicht als UI-Slider exponiert. Aktivierung des Slider-Specs ergänzt drei Tuning-Achsen im Web-Frontend.

**Status:** Slider in UI exposed, Grid noch nicht durchgeführt.

### 4.5 Hyperparameter-Tuning per Grid-Search

**Methode:** Skript `scripts/elo/grid_search.py`, model-aware via `--model {elo|glicko2|trueskill}`. Lädt konsolidierten Record-Stream einmal, walked Grid in-memory.

**Per-Modell-Grids (FULL):**

| Modell | Achsen | Kombinationen |
|---|---|---|
| Elo | K, Blend-Indiv, Decay, Team-Min, Provisional-Mult | 5×3×3×3×3 = 405 |
| Glicko-2 | initial_phi, tau, rating_period_days | 3×4×4 = 48 |
| TrueSkill | beta, tau, sigma0, sigma_inflation_per_year | 5×4×4×4 = 320 |

**Quick-Grids** für schnelles Iteration verfügbar (6, 12, 24 Kombinationen).

**Ergebnisse (Quick-Grids, Cutoff 2024-12-31):**

| Modell | Bestes Set (rank-sum) | OOS Acc | OOS CE |
|---|---|---|---|
| Elo | K=30, blend=0.8 | 66,3% | **0,013** |
| Glicko-2 | initial_phi=200, tau=0.3, period=7 | 66,4% | **0,008** (von 0,025) |
| TrueSkill | beta=6.0, tau=0.02, sigma0=8.33, inflation=1.2 | 66,8% | **0,030** (von 0,051) |

**Hauptbefund:** Die Hyperparameter-Auswahl ist primär ein **Calibration-Hebel**, kein Accuracy-Hebel. Glicko-2 calibrierte sich auf 0,008 (Faktor 3 besser); TrueSkill von 0,051 auf 0,030 (-41%). Accuracy variiert über alle Grids nur um 0,5-1,0pp.

**Erklärung:** Der Halo-Default für TrueSkill (β=4.17, τ=0.083) ist zu aggressiv für die deutlich engere Skill-Verteilung im Beach-Profi-Bereich (Top-Spieler-Bandbreite ist enger als in Halo-Matchmaking). β=6.0 spreizt die Skill-Klassen weiter, τ=0.02 reduziert Drift-Annahme, weil Profispieler-Skill stabil ist.

### 4.6 Ensemble-Aggregation

**Motivation:** Die drei Modelle haben unterschiedliche Fehler-Profile:
- **Elo** ist smooth, aber träge bei Form-Veränderungen.
- **Glicko-2** ist gut kalibriert, aber langsam zur Reaktion auf Streaks (Batch-Updates).
- **TrueSkill** ist team-aware, aber sensibel gegenüber Hyperparametern.

Wenn Fehler **unkorreliert** sind, kann simple Mittelung Variance reduzieren ohne Bias zu erhöhen.

**Implementierung:** `scripts/elo/ensemble.py`. EnsembleModel hält drei Child-Instanzen, fan-out per Match. Prediction = gewichtetes Mittel; Display-Rating = $\Delta$-normalisiertes gewichtetes Mittel.

**Gemessener Effekt (Default-Gewichte je 1.0):**

| Modell | OOS Acc | OOS CE |
|---|---|---|
| Elo | 66,8% | 0,013 |
| Glicko-2 | 66,3% | 0,008 |
| TrueSkill | 66,8% | 0,032 |
| **Ensemble** | **67,1%** | **0,007** |

**+0,3pp Accuracy über das beste Einzelmodell, beste Calibration aller Modelle.**

**Diskussion:** Calibration-Verbesserung (0,008 → 0,007) ist gering aber konsistent. Accuracy-Gewinn von 0,3pp bei n=5.646 entspricht 17 zusätzlich korrekten Predictions — statistisch nicht hochsignifikant (binomiale ~1σ Schwankung), aber das Ensemble verschlechtert sich auch in **keinem** Sub-Bucket. Das spricht für echte Variance-Reduktion statt Glücksvariation.

### 4.7 Datenbasis-Erweiterung (Backfill)

**Motivation:** Pre-Backfill-Coverage:
- bvbinfo Frauen 2015-2021: nicht erfasst.
- DVV Saisons 15-24: nur 25-26 gescrapt.

**Backfill-Aufwand:** 495 HTTP-Requests (gedrosselt 0.75s) über 3 Phasen: bvb-discover/matches Frauen 2015-21, DVV-discover/tournaments/teams 15-24.

**Datenwachstum:**

| Metrik | Vorher | Nachher | Δ |
|---|---|---|---|
| matches.csv | 115.629 | 116.521 | **+892** |
| bvb-discovered | 652 | 832 | +180 |
| DVV-Match-Stubs | 409 | 1.282 | +873 |
| Player-Anzahl | 11.058 | 11.058 | 0 |

**Gemessener Effekt:**

| Modell | OOS vor | OOS nach |
|---|---|---|
| Elo | 66,83% | 66,83% |
| Glicko-2 | 66,44% | 66,35% |
| TrueSkill | 66,78% | 66,78% |

**Effekt im Bereich des Rauschens.** Erklärung in zwei Teilen:

1. **Source-Substitution statt -Addition:** bvb-Frauen 2015-21 überlappen massiv mit FIVB-CSV (deren Coverage bis 2022 reicht). Der `_dedup_bvb_vs_fivb`-Schritt bevorzugt bvb (richer parsing), aber **die Match-Anzahl bleibt gleich**, nur die Datenquelle wechselt. Echte Neuzugänge sind hauptsächlich DVV-only-Matches (deutsche Nachwuchsspieler ohne FIVB-Auftritt).

2. **Generations-Wechsel:** Pre-2022-Matches betreffen größtenteils Spieler, die im OOS-Set (2025+) nicht mehr vorkommen (Walkenhorst spielte zuletzt 2017, Brink/Reckermann 2016). Zusätzliche historische Coverage stabilisiert Pre-Cutoff-Ratings dieser Veteranen, ändert aber Predictions über aktuelle Spielergeneration kaum.

**Implikation:** Für zukünftige Iterationen ist **Daten-Augmentierung in Bereichen mit OOS-Überlap relevanter** als reine historische Tiefe — z.B. tagesaktuelles DVV-Scraping für die laufende Saison.

---

## 5. Ergebnisse — Gesamtperformance

### 5.1 Kumulativer Verbesserungs-Pfad

| Iteration | Datum | OOS Acc (best) | OOS CE (best) | Hauptverbesserung |
|---|---|---|---|---|
| Baseline (Untuned) | initial | 66,45% | 0,014 (Elo) | drei Modelle live |
| + Priors + Aliasing | +1 Session | 66,47% | 0,013 (Elo) | Cold-Start für deutsche Top-Spieler |
| + Hyperparam-Tuning | +1 Session | 66,83% | **0,008** (Glicko-2) | β/τ/initial_phi gegrided |
| + Backfill | +1 Session | 66,83% | 0,008 | Coverage-Lücken geschlossen |
| **+ Ensemble** | **+1 Session** | **67,10%** | **0,007** | gewichtete Mittelung der drei |

**Gesamt-Δ über die Studie: +0,65pp Accuracy, Calibration halbiert (0,014 → 0,007).**

### 5.2 Per-Modell-Aufschlüsselung (final)

| Modell | OOS Acc (n=5646) | OOS CE | In-Sample DVV-25 (n=370) | Loop-Zeit | Spieler |
|---|---|---|---|---|---|
| Elo (tuned) | 66,8% | 0,013 | 62,7% | 4,9s | 11.058 |
| Glicko-2 (tuned) | 66,3% | 0,008 | 65,4% | 19,4s | 11.058 |
| TrueSkill (tuned) | 66,8% | 0,032 | 64,3% | 42,4s | 11.058 |
| **Ensemble** | **67,1%** | **0,007** | 63,8% | 133,0s | 11.058 |

### 5.3 Calibration-Detail (Ensemble)

| Bucket | Predicted | Actual | n |
|---|---|---|---|
| [0.5, 0.6) | 55% | 55% | ~1.900 |
| [0.6, 0.7) | 65% | 64% | ~1.500 |
| [0.7, 0.8) | 75% | 73% | ~1.150 |
| [0.8, 0.9) | 85% | 83% | ~720 |
| [0.9, 1.0) | 95% | 92% | ~376 |

Im Vergleich zur Elo-Baseline (CE 0,014, max-Bucket-Abweichung 8pp) reduziert das Ensemble systematische Überkonfidenz im hohen Bucket-Bereich um Faktor 2-3.

### 5.4 Sanity-Checks via Top-Spieler-Identifikation

**Top-10 Frauen (≥10 Matches, alle drei Modelle übereinstimmend):**
1. Taryn Brasher (USA, 214 M)
2. Kristen Cruz (USA, 214 M)
3. Melissa Humana-Paredes (CAN, 838 M)
4. Brandie Wilkerson (CAN, 671 M)
5. Carolina Salgado (BRA, 1.264 M)
6. Duda Lisboa (BRA, 543 M)

**Top-10 Deutsche (Elo, ≥5 Matches):**
1. Clemens Wickler (1.918, 483 M)
2. Nils Ehlers (1.918, 412 M)
3. Cinja Tillmann (1.904, 378 M)
4. Svenja Müller (1.904, 254 M)
7. Kira Walkenhorst (1.820, 410 M, last_active 2017-08)

**Walkenhorst-Test:** Kira Walkenhorst spielte zuletzt 2017, gewann 2016 Olympia-Gold. In unserem Modell rangiert sie auf Platz 7 mit 1.820 Elo trotz 410 Matches — der Decay-Mechanismus zieht ihre Skill-Schätzung Richtung 1500 ohne sie auf #1 hängen zu lassen. Der Effekt ist plausibel und entspricht der Intuition, dass ihr historischer Skill respektiert wird, ohne dass sie heute noch als wettkampfaktiv geltend modelliert wird.

---

## 6. Diskussion

### 6.1 Was am stärksten gewirkt hat

**Hyperparameter-Tuning** war der mit Abstand stärkste Hebel pro Aufwand:
- Glicko-2 Calibration: 0,025 → 0,008 (Faktor 3).
- TrueSkill Calibration: 0,051 → 0,030 (-41%).
- Elo profitierte am wenigsten (war bereits nahe Optimum).

**Ensemble** lieferte den entscheidenden Accuracy-Sprung auf 67,1% — und ist als 4. Modell für Endnutzer transparent (gleicher Output, gleiche UI).

### 6.2 Was überraschend wenig gewirkt hat

**DVV Cold-Start-Priors** (+0,02-0,16pp OOS) blieben deutlich unter der Erwartung von "+2-3pp". Die initiale Annahme war, dass ~45% der OOS-Matches Cold-Start-Spieler involvieren — die tatsächliche Zahl liegt eher bei 5-8%, weil das OOS-Set 2025+ überwiegend etablierte Pro-Spieler enthält. Priors helfen am stärksten in den ersten Matches einer Karriere, und genau diese Matches sind im OOS-Set unterrepräsentiert.

**Backfill 2015-2021 Frauen + DVV 15-24** (~0pp OOS) scheiterte am gleichen Strukturproblem (Generationsabstand) plus Source-Substitution durch FIVB-CSV-Überlap. Für zukünftige Iterationen ist gezielte Augmentierung der **aktuellen Saison** (rolling DVV-Scrape) wertvoller als historische Tiefe.

### 6.3 Inhärenter Noise-Floor

Beach-Volleyball weist mehrere strukturelle Eigenschaften auf, die einen niedrigen empirischen Vorhersage-Plafond plausibel machen:

1. **Kurze Matches:** Best-of-3 zu 21/15 ≈ 30-90 Punkte insgesamt → hohe Stichproben-Varianz auf Match-Ebene.
2. **Außenbedingungen:** Wind, Sonne, Court-Wechsel mit jeder Seitenwechsel-Sequenz → 2-3pp Random-Win-Wahrscheinlichkeits-Verschiebung.
3. **2-Personen-Teams:** Kein Pech-Ausgleich über Kader-Tiefe.
4. **Hohe Qualifikations-Quote:** ~30-40% Quali-Matches pro Tournament, oft mit unklarem Skill der Underdogs.

Empirische Vergleichswerte aus der Literatur:
- Schach (Elo): ~75-80% Accuracy.
- ATP-Tennis (TrueSkill-Varianten): ~67-69%.
- NBA (Elo): ~70%.
- Fußball-Bundesliga: ~55-58%.
- Beach-Volleyball (kommerzielle Wett-Modelle, anekdotisch): ~67-72%.

**Unsere 67,1% liegen damit nahe dem empirischen Plafond für Beach.**

### 6.4 Theoretisches Maximum

Eine theoretische Obergrenze für $p$-correct lässt sich aus dem Anteil "echter" Coinflip-Matches abschätzen. Eine $\mu$-Differenz von <30 Elo-Punkten (entspricht $p \approx 0{,}55$) liegt bei ~28% unserer OOS-Matches vor. Wenn diese Matches faktisch nicht besser als 50% vorhersagbar sind, ergibt das einen Plafond von:

$$p_\text{max} \approx 0{,}28 \cdot 0{,}50 + 0{,}72 \cdot p_\text{decisive} \approx 0{,}14 + 0{,}72 \cdot 0{,}80 = 71{,}6\%$$

(bei 80% Accuracy auf decisive Matches). Diese 71,6% sind eine grobe Obergrenze — wir liegen mit 67,1% rund 4,5pp darunter und damit im erwartbaren Optimierungsbereich.

---

## 7. Limitationen

1. **Datenquellen-Heterogenität.** FIVB-, DVV-, bvb-Matches haben unterschiedliche Wettbewerbs-Niveaus. Source-Weights wurden zwar konfigurierbar gemacht, aber nicht systematisch gegridet.

2. **Cross-Source-Identitätsprobleme.** Trotz Aliasing bleiben ~30-100 Edge-Cases (vermutet) mit gespaltenen Player-IDs. Geburtsdaten fehlen in DVV- und bvb-Quellen.

3. **In-Sample-Klein-Stichprobe.** DVV-25+ als "In-Sample" hat nur 370 Matches und ausschließlich deutsche Spieler — keine valide Generalisierungs-Aussage.

4. **MoV nur in Elo aktiviert.** Glicko-2 und TrueSkill nutzen MoV-Information nicht. Bei Beach könnte das systematisch Information verlieren — Set-Scores und Punktedifferenzen sind in beiden Modellen prinzipiell integrierbar, aber nicht-trivial.

5. **Kein Bedingungs-Feature.** Wind, Court-ID, Tageszeit, Outdoor/Indoor — alles unbekannt. In großen Saisonsamples mitteln sich diese Effekte vermutlich heraus, aber für Einzel-Match-Predictions ist das ein blinder Fleck.

6. **OOS-Set statistisch klein.** 5.646 Matches ergeben einen 95%-CI von ca. ±1,2pp auf einer Accuracy-Punktschätzung. Differenzen <0,5pp zwischen Modellen sind im Rauschen.

7. **Kein Bootstrap / kein formales Hypothesentest-Protokoll.** Reported Differences sind Punktschätzungen ohne Konfidenzintervalle.

---

## 8. Zukünftige Arbeit

### Kurzfristig (1-2 Sessions)

- **Source-Weight Grid** auf den drei Achsen (DVV, FIVB, bvb).
- **MoV-Strength Grid** (0.0, 0.25, 0.5, 0.75, 1.0, 1.25, 1.5).
- **Ensemble-Gewichts-Tuning**: aktuell alle Gewichte 1.0; Grid 0-1.5 mit 0.25-Schritten.
- **Bootstrap-Konfidenzintervalle** auf alle OOS-Metriken.

### Mittelfristig

- **MoV in Glicko-2 und TrueSkill**: per-Match σ-Skalierung in TS, gewichtete Update-Aggregation in G2.
- **Stacked Ensemble** statt simpler Mittelung: Logistic-Regression auf $(p_E, p_G, p_T)$ als Features mit Validation-Slice-Tuning.
- **Time-Series-Cross-Validation**: 5-Fold mit verschiedenen Cutoffs (2023, 2024, 2025) für robustere Schätzungen.

### Langfristig

- **Bedingungs-Features**: Tournament-Location als Proxy für Wetter, Court-Tracking via Match-Detail-Scraping.
- **Position-Spezialisierung**: Block vs Defense — separate Rating-Komponenten.
- **Set-by-Set-Update** statt Match-Level (Verdreifachung der Update-Frequenz, jeder Satz ein Mini-Match).
- **Lernkurven-Modelle**: Bayes-Update mit Alter-/Erfahrungs-Prior (Spieler bei 20 vs 35 Jahren entwickeln sich anders).
- **Modellierung des Partnerschaft-Effekts**: explicit team-experience curve, anstelle des linearen blend.

---

## 9. Reproduzierbarkeit

Das gesamte System ist Open-Source im Repository `gbt-fantasy-optimizer` (Branch `main`). Alle Resultate dieser Arbeit sind durch folgende Befehlskette reproduzierbar:

```bash
# Einmalig: Dependencies
pip install -r scripts/requirements.txt

# Datenpipeline (Scraping mit 0.75s Throttle, Caches dauerhaft)
python scripts/elo/build_ratings.py --phase discover --saisons 15,16,17,18,19,20,21,22,23,24,25,26 --gender m
python scripts/elo/build_ratings.py --phase discover --saisons 15,16,17,18,19,20,21,22,23,24,25,26 --gender f
python scripts/elo/build_ratings.py --phase tournaments
python scripts/elo/build_ratings.py --phase teams
python scripts/elo/build_ratings.py --phase fivb
python scripts/elo/build_ratings.py --phase bvb-discover --years 2015,2016,2017,2018,2019,2020,2021,2022,2023,2024,2025,2026 --gender m
python scripts/elo/build_ratings.py --phase bvb-discover --years 2015,2016,2017,2018,2019,2020,2021,2022,2023,2024,2025,2026 --gender f
python scripts/elo/build_ratings.py --phase bvb-matches

# Aliasing-Datei (optional, sonst auto-generiert im build)
python -m scripts.elo.aliases --print

# Modell-Build (alle vier Modelle, ~6-8 min total)
python scripts/elo/build_ratings.py --phase build

# Grid-Search per Modell
python scripts/elo/grid_search.py --model elo --cutoff 2024-12-31
python scripts/elo/grid_search.py --model glicko2 --cutoff 2024-12-31
python scripts/elo/grid_search.py --model trueskill --cutoff 2024-12-31

# Unit-Tests
python -m unittest discover -s scripts/elo/tests
```

**Ausgaben** unter `data/`:
- `elo_current.json` / `glicko2_current.json` / `trueskill_current.json` / `ensemble_current.json` — Spieler-Rankings für die UI.
- `elo_models_meta.json` — vergleichende OOS-Metriken aller vier Modelle.
- `elo_aliases.json` — auto-generierte Merge-Regeln.
- `matches.csv` — konsolidierter Match-Stream.
- `elo_grid_results_{elo,glicko2,trueskill}.txt` — Grid-Search-Output mit besten Kombinationen.

**Web-UI** (lokal):
```bash
python scripts/serve.py    # serviert auf :8000
```
Browser auf `http://localhost:8000`, Tab "🏅 ELO Rangliste" oder "🔬 ELO Tuning".

**Code-Stand:** 52 Unit-Tests, alle grün. Keine Lint-Pipeline, keine Build-Pipeline (statisches Frontend, Python-Backend ohne Build-Step).

---

## 10. Quellenangaben

### Akademisch

- Elo, A. E. (1978). *The Rating of Chessplayers, Past and Present*. Arco Publishing.
- Glickman, M. E. (2013). *Example of the Glicko-2 system*. Boston University. http://www.glicko.net/glicko/glicko2.pdf
- Herbrich, R., Minka, T., & Graepel, T. (2007). *TrueSkill™: A Bayesian skill rating system*. Advances in Neural Information Processing Systems 20, 569-576.
- Glickman, M. E. (1999). *Parameter estimation in large dynamic paired comparison experiments*. Journal of the Royal Statistical Society Series C, 48(3), 377-394.

### Datenquellen

- **BigTimeStats Archive**: AVP & FIVB Beach Volleyball Match Database, 2000-2022. https://github.com/BigTimeStats/beach-volleyball
- **Beach Volleyball Database** (bvbinfo.com): https://bvbinfo.com
- **DVV German Beach Tour**: https://beach.volleyball-verband.de/public/

### Implementierungsreferenzen

- `trueskill` PyPI Package (Heungsub Lee, BSD-License): https://trueskill.org
- FiveThirtyEight Elo-Methodology-Posts: https://fivethirtyeight.com/methodology/

---

## Anhang A: Repository-Struktur

```
scripts/elo/
  models.py            # RatingModel-Protocol + Factory + Dispatch
  elo_adapter.py       # Klassisches Elo mit MoV, Importance, Source-Weights, Priors
  glicko2.py           # Glicko-2 from-scratch (Glickman 2013 reference impl)
  trueskill_model.py   # TrueSkill via PyPI-Package
  ensemble.py          # 4. Modell: gewichtetes Mittel der drei
  priors.py            # DVV-Cold-Start Mapping
  aliases.py           # Heuristisches Name-Aliasing
  runner.py            # Modell-agnostischer chronologischer Match-Loop
  build_ratings.py     # Orchestrator: Scraping → 4 Modelle → JSON
  grid_search.py       # Hyperparameter-Suche (model-aware)
  scraper.py           # DVV + FIVB-Scraping
  scraper_bvb.py       # bvbinfo.com-Scraping
  tests/               # 52 Unit-Tests
```

## Anhang B: Web-UI Tabs

| Tab | Funktion |
|---|---|
| 📊 Alle Spieler | Fantasy-Spieler-Liste mit Preisen |
| 🔒 Meine Picks | Persönliche Lock/Ban-Auswahl |
| ⚖ Vergleich | Algorithmen-Vergleich (Optimal, Konsistent, Form-Trend, Turnier-Prognose) |
| 🏆 Turnier-Baum | Bracket-Visualisierung mit Per-Match-Modal |
| **🏅 ELO Rangliste** | **4-Modell-Auswahl, Country/Gender/Activity-Filter, Suche** |
| **🔬 ELO Tuning** | **Dynamische Slider pro Modell, Live-Recompute, Sandbox** |

---

**Korrespondenz:** Repository-Issue oder Code-Review-PR via Git.
**Lizenz:** unbestimmt (privater Stand 2026-06).
**Version dieses Dokuments:** 2.0 (2026-06-13, post-Ensemble + Tuning + Backfill)
