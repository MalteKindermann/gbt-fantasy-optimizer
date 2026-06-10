// Frontend-Konfiguration. Wird vor app.js geladen.
//
// **In Self-Host bleibt diese Datei wie unten committed** — alle Werte leer,
// d.h. relative API-Pfade und kein Login-Overlay. App startet sofort.
//
// **In Cloud-Deploy** wird die Datei zum Build-Zeitpunkt von Vercel via
// `scripts/generate-config.js` aus Env-Vars überschrieben (API_BASE,
// SUPABASE_URL, SUPABASE_ANON_KEY). Die echten Werte landen NIE in Git.

(function () {
    const host = location.hostname;
    const isLocal = host === 'localhost' || host === '127.0.0.1' || host === '';

    window.API_BASE          = isLocal ? '' : '';
    window.SUPABASE_URL      = '';
    window.SUPABASE_ANON_KEY = '';
})();
