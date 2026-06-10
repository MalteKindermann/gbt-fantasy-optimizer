# 🏐 GBT Fantasy Optimizer

Ein Tool, das ein statistisch optimales Fantasy-Team für die [German Beach Tour](https://gbt-fantasy.web.app/) berechnet — basierend auf Spieler-Stats, aktueller DVV-Rangliste, Head-to-Head-Bilanzen und Live-Bracket-Daten des nächsten Turniers.

## Features

- **Mehrere Optimierungs-Algorithmen** parallel — reines Punktemaximum, konsistente Performer (Bayes-Shrinkage), Turnier-Prognose (Monte-Carlo), Final-Fokus
- **Kapitän-Logik** (1.5× Punkte) wird im Solver mitberechnet
- **Picks & Bans**: einzelne Spieler erzwingen oder ausschließen, alle Algorithmen respektieren das
- **Turnier-Bracket-Vorhersage** über DVV-Setzliste + Head-to-Head-Daten, mit manuell anpassbaren Match-Ergebnissen
- **Auto-Sync** der Preise aus dem offiziellen Fantasy-Backend (Firestore)
- **Vergleichs-Tab** zeigt alle Algorithmus-Ergebnisse nebeneinander

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

1. Auf <https://gbt-fantasy.web.app/> mit deinem Google-Account einloggen
2. Browser-DevTools öffnen (F12) → Console
3. Inhalt von `fetch_auth_token.txt` einfügen und ausführen — das lädt eine `firebase_auth.json` herunter
4. Datei nach `data/firebase_auth.json` legen (oder die zwei Werte in eine `.env.local` schreiben, Vorlage in `.env.local.example`)
5. Server neu starten

Ohne diesen Schritt funktioniert das Tool weiterhin — du verlierst nur den automatischen Preis-Sync und kannst Preise stattdessen manuell über den "Preise eintragen"-Dialog setzen.

## Wie's funktioniert (Kurz)

- **Python-Backend** (`scripts/serve.py`) liefert statische Dateien aus und stellt eine kleine JSON-API für Simulation, Sync und Preis-Updates bereit
- **Frontend** (`index.html` + `app.js` + `styles.css`) ist Vanilla-JS, kein Build-Step
- **Scraper** holen sich Daten von [beach.volleyball-verband.de](https://beach.volleyball-verband.de/public/) (DVV-Setzliste, Spielplan, Ranglisten) und [gbt.hanski.de](https://gbt.hanski.de/) (H2H-Bilanzen) — alles mit lokalem Disk-Cache, damit man die Quellen nicht hämmert
- **Monte-Carlo-Simulation** läuft pro Bracket-Position und produziert die `expectedMatches`-Werte, die der "Turnier-Prognose"-Algorithmus nutzt

Eine ausführliche Architektur-Übersicht inkl. Datenfluss, Algorithmen und Konventionen steht in [`CLAUDE.md`](CLAUDE.md).

## Hosted Version

Eine gehostete Variante läuft unter <https://gbt-fantasy-optimizer.vercel.app>. Login ist invite-only — wenn du Zugriff willst, melde dich bei mir.

Die hosted Version teilt sich denselben Code wie die Self-Host-Variante; Cloud-Mode (Frontend auf Vercel, Backend auf Google Cloud Run mit GCS-Volume, Auth via Supabase) aktiviert sich nur wenn Build-Time-Env-Vars gesetzt sind. Für lokale Nutzung ist davon nichts spürbar.

## Lizenz

Privatprojekt. Use at your own risk — die Daten kommen von Drittquellen, ich übernehme keine Garantie für Korrektheit.
