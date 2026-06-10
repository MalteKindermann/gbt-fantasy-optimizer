// Frontend-Konfiguration. Wird vor app.js geladen.
//
// Default: alle Werte leer = lokaler Self-Host-Modus ohne Login.
// Frontend ruft `/api/*` und `/data/*` relativ vom selben Server ab,
// kein Supabase-Init, kein Login-Overlay.
//
// Cloud-Deploy (Vercel + Fly + Supabase): die drei Werte unten setzen.
// Diese Datei ist der einzige Touch-Point.

(function () {
    const host = location.hostname;
    const isLocal = host === 'localhost' || host === '127.0.0.1' || host === '';

    // Backend-URL. Leer => relativ (Self-Host). Sonst: Fly-App-URL ohne trailing slash.
    window.API_BASE = isLocal ? '' : '';

    // Supabase-Auth. Leer => Auth deaktiviert, App startet ohne Login.
    window.SUPABASE_URL      = '';
    window.SUPABASE_ANON_KEY = '';
})();
