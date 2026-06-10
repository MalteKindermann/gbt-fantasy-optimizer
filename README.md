# 🏐 GBT Fantasy Team Optimizer

Statistisch optimales Team-Tool für die German Beach Tour 2025 Fantasy Liga.

## 🚀 Installation

### Schnellstart (einfach öffnen)
1. Lade alle Dateien herunter
2. Öffne `index.html` direkt im Browser
3. Fertig! Keine Installation notwendig.

### Mit lokalem Server (empfohlen für Entwicklung)
```bash
# Python 3
python -m http.server 8000

# Node.js
npx serve

# PHP
php -S localhost:8000
```

Dann öffne: `http://localhost:8000`

## 📁 Projektstruktur

```
gbt-fantasy-optimizer/
├── index.html          # Hauptseite (HTML Struktur)
├── styles.css          # Komplettes Styling
├── app.js              # Hauptlogik & Optimierungsalgorithmus
├── data.js             # Spielerdatenbank (hier Preise updaten!)
└── README.md           # Diese Datei
```

## ✏️ Spielerdaten aktualisieren

Öffne `data.js` und ändere die Werte:

```javascript
"Spielername": {
    coins: 35,           // Preis in Coins
    pos: "Abwehr",       // Position: "Block", "Abwehr", oder "Hybrid"
    gender: "W",         // "M" oder "W"
    tp: 842.9,           // Total Points (Gesamtpunkte)
    t: 20,               // Anzahl Turniere
    mp: 60               // Anzahl Matches
}
```

### Neue Spieler hinzufügen:
```javascript
"Neuer Spieler": {
    coins: 25,
    pos: "Block",
    gender: "M",
    tp: 450.5,
    t: 12,
    mp: 30
}
```

## 🎮 Features

### 3 Tabs:
- **📊 Alle Spieler** - Vollständige Übersicht mit Stats
- **⭐ Optimales Team** - Automatisch berechnetes bestes Team
- **💎 Effizienz-Ranking** - Sortiert nach Punkten pro Coin

### Einstellbare Parameter:
- **Budget** - Maximale Coins (Standard: 240)
- **Teamgröße** - Anzahl Spieler (Standard: 6)
- **Max Block** - Max. Block-Spieler (0 = egal)
- **Max Abwehr** - Max. Abwehr-Spieler (0 = egal)
- **Filter** - Nach Position oder Gender filtern

### Statistiken pro Spieler:
- Gesamtpunkte
- Ø pro Turnier
- Ø pro Match
- **Effizienz (Punkte/Coin)** ← Wichtigster Wert!

## 🧮 Optimierungsalgorithmus

Der Optimizer nutzt einen **Greedy-Algorithmus**:

1. Sortiere alle Spieler nach Effizienz (Punkte pro Coin)
2. Wähle die effizientesten Spieler aus, die:
   - Im Budget liegen
   - Die Positions-Limits einhalten
   - Punkte haben (tp > 0)
3. Zeige Team-Zusammensetzung und Gesamtstatistiken

### Positions-Limits:
- **0 eingeben** = keine Beschränkung (egal)
- **Zahl > 0** = Maximum dieser Position

Beispiel: `Max Block = 0, Max Abwehr = 0` → Alle Positionen erlaubt!

## 🌐 Hosting

### GitHub Pages (kostenlos)
1. Erstelle ein GitHub Repository
2. Lade alle Dateien hoch
3. Gehe zu Settings → Pages
4. Wähle Branch: `main`, Folder: `/root`
5. Fertig! URL: `https://username.github.io/repository/`

### Netlify (kostenlos)
1. Registriere dich auf netlify.com
2. Drag & Drop den kompletten Ordner
3. Fertig! Du bekommst eine URL

### Vercel (kostenlos)
1. Registriere dich auf vercel.com
2. Import GitHub Repo oder Drag & Drop
3. Fertig!

## 🛠️ Technologie

- **Vanilla JavaScript** - Keine Frameworks nötig
- **CSS Grid & Flexbox** - Responsive Layout
- **Google Fonts** - Outfit & JetBrains Mono
- **Animations** - Smooth CSS Transitions

## 📱 Browser-Support

- Chrome/Edge ✅
- Firefox ✅
- Safari ✅
- Mobile Browser ✅

## 💡 Tipps

1. **Effizienz ist König**: Spieler mit vielen Punkten pro Coin sind am wertvollsten
2. **0 = egal**: Setze Positions-Limits auf 0 für maximale Flexibilität
3. **Budget anpassen**: Erhöhe das Budget wenn kein Team gefunden wird
4. **Daten aktuell halten**: Update `data.js` nach jedem Turnier

## 📝 Lizenz

Frei verwendbar für private Zwecke.

## 🐛 Probleme?

Die App funktioniert komplett offline im Browser. Bei Problemen:
1. Browser-Cache leeren (Strg+F5)
2. Console öffnen (F12) für Fehler
3. `data.js` auf Syntax-Fehler prüfen (Komma fehlt?)

---

Viel Erfolg mit deinem Fantasy Team! 🏐🏆
