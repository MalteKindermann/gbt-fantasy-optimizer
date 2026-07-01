# 🏐 GBT Fantasy Optimizer

Ein Tool, das ein statistisch optimales Fantasy-Team für die [German Beach Tour](https://gbt-fantasy.web.app/) berechnet — basierend auf Spieler-Stats, aktueller DVV-Rangliste, Head-to-Head-Bilanzen, einem eigenen ELO-Ratingsystem und Live-Bracket-Daten des nächsten Turniers.

## Features

- **Mehrere Optimierungs-Algorithmen** parallel: reines Punktemaximum (*Optimal*), konsistente Performer (*Konsistent*, Varianz/Bayes-Shrinkage), *Form-Trend* (letzte Turniere stärker gewichtet) — plus pro Turnierbaum je eine *Turnier-Prognose* und einen *Finale-Fokus*.
- **Vier wählbare Turnierbaum-Vorhersagen** (für Männer *und* Frauen):
  - **Aktuell** — Head-to-Head → DVV-Punkte → Setzliste
  - **DVV-Punkte** — Siegchance aus dem Punkteverhältnis
  - **ELO** — Siegchance aus den ELO-Ratings (Modell wählbar: ELO/Glicko-2/TrueSkill/Ensemble)
  - **Persönlich** — der aktuelle Baum plus deine manuell gesetzten Sieger

  Bereits gespielte Spiele bleiben in jedem Baum auf dem echten Ergebnis fixiert.
- **Eigenes Ratingsystem** (🏅 ELO-Rangliste): vier Modelle (ELO, Glicko-2, TrueSkill, Ensemble) aus 116k+ Matches (DVV German Beach Tour, FIVB, bvbinfo.com). Eigener Ranglisten-Tab mit Filtern; die Ratings fließen als ELO-Turnierbaum in die Optimierung ein.
- **Kapitän-Logik** (1.5× Punkte) wird im Solver mitberechnet.
- **Picks & Bans**: einzelne Spieler erzwingen oder ausschließen — alle Algorithmen respektieren das.
- **Vergleichs-Tab**: alle Algorithmus-Panels nebeneinander, per Drag & Drop sortierbar und einzeln ausblendbar (Reihenfolge wird gespeichert).
- **Match-Detail-Modal**: Klick auf ein Spiel im Turnierbaum zeigt H2H-Bilanzen, DVV-Punkte-Herkunft und — im ELO-Baum — wie das Team-Rating und die Siegwahrscheinlichkeit berechnet wurden.
- **Auto-Sync** der Preise aus dem offiziellen Fantasy-Backend (Firestore).

## Selbst hosten

Voraussetzung: Python 3.10+.

```bash
git clone https://github.com/MalteKindermann/gbt-fantasy-optimizer.git
cd gbt-fantasy-optimizer
pip install -r scripts/requirements.txt
python scripts/serve.py
```

Dann im Browser auf <http://localhost:8000>. Kein Login, kein Cloud-Account, nichts.

### Optional: Live-Preise und Rookie-Daten

Beim ersten Start ist die Spielerliste leer (kein laufendes Turnier registriert). Sobald ein GBT-Turnier ansteht oder läuft, lädt der eingebaute Scraper das Bracket automatisch. Damit auch Preise und neue Spieler aus dem offiziellen [gbt-fantasy.web.app](https://gbt-fantasy.web.app/)-Backend gezogen werden, brauchst du einmal einen Firebase-Refresh-Token:

1. Auf <https://gbt-fantasy.web.app/> mit deinem Account einloggen
2. Browser-DevTools öffnen (F12) → Console
3. Inhalt von `fetch_auth_token.txt` einfügen und ausführen — das lädt eine `firebase_auth.json` herunter
4. Datei nach `data/firebase_auth.json` legen (oder die zwei Werte in eine `.env.local` schreiben, Vorlage in `.env.local.example`)
5. Server neu starten

Ohne diesen Schritt funktioniert das Tool weiterhin — du verlierst nur den automatischen Preis-Sync und kannst Preise stattdessen manuell über den "Preise eintragen"-Dialog setzen.

### Optional: ELO-Ratings bauen

Die 🏅 ELO-Rangliste und der ELO-Turnierbaum brauchen einmal gebaute Rating-Dateien. Alle Modelle werden **lokal** berechnet (die gehostete Version rechnet nie selbst — sie liest nur fertige Dateien).

**Ein Befehl reicht** — er lädt fehlende Python-Pakete, scrapt die Turnierdaten und baut alle Modelle, mit Fortschrittsanzeige:

- **Windows:** Doppelklick auf **`setup_elo.bat`** (oder im Terminal `setup_elo.bat`)
- **macOS / Linux:** `chmod +x setup_elo.sh` (einmalig), dann **`./setup_elo.sh`**
- **Überall:** `python scripts/elo/setup.py`

Der erste Lauf lädt viel Historie (höfliches Rate-Limit) und dauert ~15–40 Min; alles wird gecached, ein erneuter Start überspringt bereits geladene Daten. Schneller Einstieg mit weniger Verlauf: `--quick` anhängen (z. B. `setup_elo.bat --quick`). Nur anschauen, was passieren würde: `--dry-run`.

Danach die App starten (`python scripts/serve.py`) — die 🏅-Rangliste und der ELO-Turnierbaum zeigen dann echte Daten. Details + Methodik in [`zusammenfassung.md`](zusammenfassung.md).

<details>
<summary><strong>Für mehr Kontrolle</strong> — die einzelnen Schritte manuell</summary>

Das Setup ruft der Reihe nach diese Phasen von `build_ratings.py` auf (Jahres-Listen sind **komma-separiert**):

```bash
# Turnierdaten scrapen (einmalig, gecached)
python scripts/elo/build_ratings.py --phase discover --saisons 15,16,17,18,19,20,21,22,23,24,25,26 --gender m
python scripts/elo/build_ratings.py --phase discover --saisons 15,16,17,18,19,20,21,22,23,24,25,26 --gender f
python scripts/elo/build_ratings.py --phase tournaments
python scripts/elo/build_ratings.py --phase teams
python scripts/elo/build_ratings.py --phase fivb
python scripts/elo/build_ratings.py --phase bvb-discover --years 2015,2016,2017,2018,2019,2020,2021,2022,2023,2024,2025,2026 --gender m
python scripts/elo/build_ratings.py --phase bvb-discover --years 2015,2016,2017,2018,2019,2020,2021,2022,2023,2024,2025,2026 --gender f
python scripts/elo/build_ratings.py --phase bvb-matches

# Modelle offline bauen (kein Netz) — schreibt die *_current.json + Meta-Dateien
python scripts/elo/build_ratings.py --phase build
```
</details>

## Wie's funktioniert (Kurz)

- **Python-Backend** (`scripts/serve.py`) liefert statische Dateien aus und stellt eine kleine JSON-API für Simulation, Sync, Preis-Updates und die ELO-Endpunkte bereit
- **Frontend** (`index.html` + `app.js` + `styles.css`) ist Vanilla-JS, kein Build-Step
- **Scraper** holen sich Daten von [beach.volleyball-verband.de](https://beach.volleyball-verband.de/public/) (DVV-Setzliste, Spielplan, Ranglisten) und [gbt.hanski.de](https://gbt.hanski.de/) (H2H-Bilanzen) — alles mit lokalem Disk-Cache, damit man die Quellen nicht hämmert
- **Monte-Carlo-Simulation** liefert die Sim-Werte pro Bracket-Position; die Turnierbäume (DVV/ELO/Aktuell/Persönlich) werden daraus im Browser deterministisch abgeleitet
- **ELO-Subpaket** (`scripts/elo/`) ist ein eigenständiges Ratingsystem und hängt nicht am Fantasy-Optimizer

Eine ausführliche Architektur-Übersicht inkl. Datenfluss, Algorithmen und Konventionen steht in [`CLAUDE.md`](CLAUDE.md).

## Hosted Version

Eine gehostete Variante läuft unter <https://gbt-fantasy-optimizer.vercel.app>. Login ist invite-only — wenn du Zugriff willst, melde dich bei mir.

Die hosted Version teilt sich denselben Code wie die Self-Host-Variante; Cloud-Mode (Frontend auf Vercel, Backend auf Google Cloud Run mit GCS-Volume, Auth via Supabase) aktiviert sich nur, wenn Build-Time-Env-Vars gesetzt sind. Für lokale Nutzung ist davon nichts spürbar.

In der Cloud werden ELO-Ratings **nie** berechnet — sie werden lokal gebaut und per Terminal-Befehl (`python scripts/elo/publish.py`) in den Cloud-Speicher hochgeladen. Die gehostete App zeigt sie nur an.

## Lizenz

Privatprojekt. Use at your own risk — die Daten kommen von Drittquellen, ich übernehme keine Garantie für Korrektheit.
