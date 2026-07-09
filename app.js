// GBT Fantasy Team Optimizer

// ── API base + Auth ───────────────────────────────────────────────────────────
// Both come from `window.*` set by config.js (loaded before this script). When
// SUPABASE_URL is empty, auth is fully disabled (Self-Host default). When
// API_BASE is empty, fetches are relative (same-origin) — same default.

const API_BASE     = (window.API_BASE     || '').replace(/\/$/, '');
const SUPABASE_URL = window.SUPABASE_URL  || '';
const SUPABASE_KEY = window.SUPABASE_ANON_KEY || '';
const AUTH_ENABLED = !!(SUPABASE_URL && SUPABASE_KEY);

// Lazily initialised Supabase client. Null when auth is disabled.
const supa = AUTH_ENABLED && window.supabase
    ? window.supabase.createClient(SUPABASE_URL, SUPABASE_KEY, {
        auth: { persistSession: true, autoRefreshToken: true },
    })
    : null;

// ── Role-based permissions ─────────────────────────────────────────────────
// Roles flow from JWT (`app_metadata.role`, set via Supabase Dashboard).
// Self-Host (no auth) acts as admin so every check is a no-op.
const ROLE_ORDER = { elo_viewer: 0, elo_lab: 1, admin: 2 };
const DEFAULT_ROLE = 'elo_viewer';

function _decodeRole(session) {
    if (!AUTH_ENABLED) return 'admin';
    const raw = session?.user?.app_metadata?.role;
    return (raw in ROLE_ORDER) ? raw : DEFAULT_ROLE;
}

function roleAtLeast(have, need) {
    return (ROLE_ORDER[have] ?? -1) >= (ROLE_ORDER[need] ?? 99);
}

// Set early so any pre-login code that touches USER_ROLE doesn't see undefined.
window.USER_ROLE = AUTH_ENABLED ? DEFAULT_ROLE : 'admin';

// Wraps fetch():
//  • Prefixes /api/* and data/* paths with API_BASE (if set).
//  • Attaches Authorization: Bearer <token> when a Supabase session exists.
//  • Shows a one-time banner on 403 ("forbidden") so the user understands why.
async function apiFetch(path, opts = {}) {
    let url = path;
    if (API_BASE && (path.startsWith('/api/') || path.startsWith('/data/') || path.startsWith('data/'))) {
        const p = path.startsWith('/') ? path : '/' + path;
        url = API_BASE + p;
    }
    const headers = new Headers(opts.headers || {});
    if (supa) {
        try {
            const { data } = await supa.auth.getSession();
            const tok = data?.session?.access_token;
            if (tok) headers.set('Authorization', 'Bearer ' + tok);
        } catch (_) { /* no session — request proceeds unauthenticated */ }
    }
    const res = await fetch(url, { ...opts, headers });
    if (res.status === 403) _showForbiddenBanner();
    return res;
}

function _showForbiddenBanner() {
    if (sessionStorage.getItem('authBannerShown') === '1') return;
    sessionStorage.setItem('authBannerShown', '1');
    let el = document.getElementById('authBanner');
    if (!el) {
        el = document.createElement('div');
        el.id = 'authBanner';
        el.style.cssText = 'position:fixed;top:0;left:0;right:0;z-index:9999;'
            + 'background:#7a2a2a;color:#fff;padding:0.6rem 1rem;text-align:center;'
            + 'font-size:0.9rem;box-shadow:0 2px 6px rgba(0,0,0,0.4)';
        el.innerHTML = 'Keine Berechtigung für diese Aktion — frage deinen Admin. '
            + '<a href="#" style="color:#ffd;text-decoration:underline;margin-left:0.5rem" '
            + 'onclick="this.parentElement.remove();return false;">schließen</a>';
        document.body.appendChild(el);
    }
}

async function logoutUser() {
    if (!supa) return;
    try { await supa.auth.signOut(); } catch (_) {}
    // onAuthStateChange handler will re-show the login overlay.
}

let allPlayers = [];
let availablePlayers = [];
let optimalTeam = null;
let tournamentSim = null;  // loaded from tournament_sim.json if available
let comparisonResults = null;  // {[alg]: {team, totals, ...}}

// Manual bracket override state
let manualMode = false;
let _manualOverridesCache = null;  // invalidated on gender/tournamentId change

// Locked (pre-selected, must-pick) players for the Picks tab
let lockedPlayerIds = new Set(JSON.parse(localStorage.getItem('lockedPlayerIds') || '[]'));
// Banned (must-exclude) players — algorithm may not pick them
let bannedPlayerIds = new Set(JSON.parse(localStorage.getItem('bannedPlayerIds') || '[]'));
// Whether to apply locks/bans during optimization (controlled by compare-tab toggle)

// ── Data loading ──────────────────────────────────────────────────────────────

// Reads a Firestore-typed value (stringValue / integerValue / doubleValue / ...)
function fsVal(field) {
    if (!field) return undefined;
    if ('stringValue'    in field) return field.stringValue;
    if ('integerValue'   in field) return Number(field.integerValue);
    if ('doubleValue'    in field) return field.doubleValue;
    if ('booleanValue'   in field) return field.booleanValue;
    if ('timestampValue' in field) return field.timestampValue;
    return undefined;
}

// Parse one raw Firestore season doc (`data/players_season_<year>.json` or
// the legacy `data/players_season.json`) into a flat per-id dict. Returns
// null if the file is missing/unparseable.
async function loadOneSeasonFile(path) {
    try {
        const res = await apiFetch(path + '?t=' + Date.now());
        if (!res.ok) return null;
        const doc = await res.json();
        const pl = doc?.fields?.pl?.mapValue?.fields || {};
        const out = {};
        for (const [id, wrap] of Object.entries(pl)) {
            const f = wrap?.mapValue?.fields || {};
            out[id] = {
                tp:  fsVal(f.tp),
                t:   fsVal(f.t),
                mp:  fsVal(f.mp),
                pos: fsVal(f.pos),
                fn:  fsVal(f.fn),
                ln:  fsVal(f.ln),
                g:   fsVal(f.g),
                ip:  fsVal(f.ip),
            };
        }
        return out;
    } catch (e) {
        console.warn(`${path} konnte nicht geladen werden:`, e);
        return null;
    }
}

// Loads ALL season-overlay files: `data/players_season_<year>.json` for every
// year in 2025..(current+1). Returns:
//   { byYear: {YYYY: {id: {tp,t,mp,pos,fn,ln,g,ip}}}, years: [...] }
// Used by loadPlayerData to build the roster and SUM stats across years —
// no more single-overlay-on-top-of-players_all-static-file design.
const EARLIEST_SEASON_YEAR = 2025;
async function loadAllSeasonOverlays() {
    const currentYear = new Date().getFullYear();
    const probes = [];
    for (let y = EARLIEST_SEASON_YEAR; y <= currentYear + 1; y++) {
        probes.push(loadOneSeasonFile(`data/players_season_${y}.json`)
            .then(d => ({ year: y, data: d })));
    }
    const settled = await Promise.all(probes);
    const byYear = {};
    const years = [];
    for (const { year, data } of settled) {
        if (data && Object.keys(data).length) {
            byYear[year] = data;
            years.push(year);
        }
    }
    // Backward-compat: if no year-suffixed files exist (e.g. fresh checkout
    // pre-firestore-sync), try the legacy single-file `players_season.json`
    // and treat it as the current year's overlay.
    if (years.length === 0) {
        const legacy = await loadOneSeasonFile('data/players_season.json');
        if (legacy && Object.keys(legacy).length) {
            byYear[currentYear] = legacy;
            years.push(currentYear);
        }
    }
    years.sort();
    window.__seasonMeta = {
        years,
        counts: Object.fromEntries(years.map(y => [y, Object.keys(byYear[y]).length])),
    };
    return { byYear, years };
}

// Re-fetches & rebuilds player tables from JSON. Does NOT trigger sim.
//
// Data model (since the Firestore-multi-year migration):
//   • Roster (which players exist) comes from the UNION of all season-overlay
//     files (data/players_season_<year>.json). data/players_all.json is
//     optional and only used as an identity-fallback for players that no
//     overlay covers (very rare — historical-only entries).
//   • Stats (tp / t / mp) are SUMMED across all overlay years. No more
//     "players_all + one overlay" addition — that double-counted as soon as
//     we cached more than one year.
async function loadPlayerData() {
    const cacheBust = '?t=' + Date.now();
    const [allRes, availRes, overlays, histRes] = await Promise.all([
        // players_all.json is OPTIONAL now — 404 is fine.
        apiFetch('data/players_all.json' + cacheBust),
        apiFetch('data/players_available.json' + cacheBust),
        loadAllSeasonOverlays(),
        // player_history.json is also optional — only present when firestore_sync
        // succeeded with valid auth. Frontend gracefully degrades without it.
        apiFetch('data/player_history.json' + cacheBust),
    ]);
    window.playerHistory = histRes.ok ? await histRes.json() : {};
    if (!availRes.ok) throw new Error('HTTP error loading players_available.json');
    const rawAll   = allRes.ok ? await allRes.json() : [];
    const rawAvail = await availRes.json();

    const yearsAsc  = overlays.years.slice();      // ascending
    const yearsDesc = yearsAsc.slice().reverse();

    // Collect every known player id (union of all overlay years + legacy players_all)
    const allIds = new Set();
    for (const y of yearsAsc) Object.keys(overlays.byYear[y]).forEach(id => allIds.add(id));
    rawAll.forEach(p => allIds.add(p.id));

    // Build the canonical roster: identity from the LATEST year overlay that
    // has fn/ln for this id, falling back to players_all.json. Stats (tp/t/mp)
    // are summed across all overlay years.
    const fullRawAll = [];
    // Overlay-backed players own their name. players_all.json is only an identity
    // fallback (see above), so a legacy-only id that duplicates an overlay player's
    // name is a stale duplicate — it must NOT shadow the real, data-carrying id in
    // name→id resolution (would steal the price and show as a dataless duplicate row,
    // e.g. two "Kalle Pieper" ids where only the overlay one has stats). Overlay ids
    // are inserted into allIds first, so they're processed (and claim the name) before
    // any legacy-only id below.
    const claimedOverlayNames = new Set();
    const normNameKey = (fn, ln) => `${fn} ${ln}`.trim().toLowerCase().replace(/\s+/g, ' ');
    for (const id of allIds) {
        // Identity (firstName / lastName / pos / gender / img)
        let identity = null, fromOverlay = false;
        for (const y of yearsDesc) {
            const ov = overlays.byYear[y][id];
            if (ov && ov.fn && ov.ln) {
                identity = {
                    firstName: ov.fn,
                    lastName:  ov.ln,
                    pos:       ov.pos || 'Hybrid',
                    gender:    ov.g   || 'M',
                    img:       ov.ip  || '',
                };
                fromOverlay = true;
                break;
            }
        }
        if (!identity) {
            const legacy = rawAll.find(p => p.id === id);
            if (legacy) {
                identity = {
                    firstName: legacy.firstName,
                    lastName:  legacy.lastName,
                    pos:       legacy.pos || 'Hybrid',
                    gender:    legacy.gender || 'M',
                    img:       legacy.img || '',
                };
            }
        }
        if (!identity) continue;   // no identity anywhere → skip

        const nameKey = normNameKey(identity.firstName, identity.lastName);
        if (fromOverlay) {
            claimedOverlayNames.add(nameKey);
        } else if (claimedOverlayNames.has(nameKey)) {
            continue;              // stale legacy-only duplicate of an overlay player
        }

        // Sum stats across all available year overlays
        let tp = 0, t = 0, mp = 0;
        for (const y of yearsAsc) {
            const ov = overlays.byYear[y][id];
            if (ov) {
                tp += ov.tp ?? 0;
                t  += ov.t  ?? 0;
                mp += ov.mp ?? 0;
            }
        }
        fullRawAll.push({ id, ...identity, tp, t, mp });
    }

    // Index for name → id resolution (used when players_available.json has
    // legacy `name` entries instead of `id`).
    const norm = (s) => s.trim().toLowerCase().replace(/\s+/g, ' ');
    const nameToId = new Map();
    fullRawAll.forEach(p => nameToId.set(norm(p.firstName + ' ' + p.lastName), p.id));

    const priceMap = new Map();
    const unknown = [];
    rawAvail.forEach(a => {
        if (a.id) {
            priceMap.set(a.id, a.price);
        } else if (a.name) {
            const id = nameToId.get(norm(a.name));
            if (id) priceMap.set(id, a.price);
            else    unknown.push(a.name);
        }
    });

    if (unknown.length) console.warn('players_available.json: Unbekannte Spieler:', unknown);
    if (overlays.years.length) {
        console.info(`Season-Overlays geladen: ${overlays.years.join(', ')} `
                   + `(${fullRawAll.length} Spieler total)`);
    }

    allPlayers = fullRawAll.map(p => buildPlayer(p, priceMap.get(p.id) ?? null));
    availablePlayers = allPlayers.filter(p => p.price !== null && p.price > 0);
    computePoolEstimates(availablePlayers);
    // History-derived metrics — constant per player, so compute once at load.
    // Re-run in runOptimizePipeline isn't needed but harmless if added later.
    computeVarianceScore(allPlayers);
    computeFormScore(allPlayers);
    computeSideOutMetrics(allPlayers);

    // Stash for sync-warning rendering
    window.__playerDataMeta = {
        unknown,
        pending: rawAvail
            .filter(a => a.name && (a.price === null || a.price <= 0))
            .map(a => a.name),
    };

    // Re-apply sim data if we have it
    applySimData();

    renderPlayers();

    // If we already loaded sim, re-render warning banners (they include data-meta now)
    if (tournamentSim) showSyncWarnings(tournamentSim.syncInfo);
}

// Initial app entry point — loads data and triggers sim freshness check
async function loadData() {
    // Fantasy data (players_available.json, tournament_sim.json,
    // player_history.json, season overlays) is admin-only on the backend.
    // Non-admins land on the ELO ranking tab which loads its own data.
    if (!roleAtLeast(window.USER_ROLE, 'admin')) {
        const eloBtn = Array.from(document.querySelectorAll('.tab'))
            .find(b => (b.getAttribute('onclick') || '').includes("switchTab('elo'"));
        if (eloBtn) switchTab('elo', eloBtn);
        return;
    }
    try {
        await loadPlayerData();
        await ensureTournamentSim();
        // After sim runs, the available list may have been rewritten — reload once more
        await loadPlayerData();
    } catch (err) {
        document.querySelector('.container').innerHTML = `
            <div style="padding:2rem;text-align:center">
                <h2 style="color:var(--danger)">Ladefehler</h2>
                <p style="color:var(--text-dim);margin-top:1rem">
                    Die JSON-Dateien konnten nicht geladen werden.<br>
                    Bitte starte den Server:<br><br>
                    <code style="background:var(--bg-dark);padding:0.4rem 0.8rem;border-radius:6px">python scripts/serve.py</code>
                </p>
            </div>`;
    }
}

// ── Tournament-sim lifecycle ─────────────────────────────────────────────────

function showSimBanner(msg, mode = 'loading') {
    let el = document.getElementById('simBanner');
    if (!el) {
        el = document.createElement('div');
        el.id = 'simBanner';
        el.className = 'sim-banner';
        document.querySelector('.container').insertBefore(el, document.querySelector('.controls'));
    }
    const spinner = mode === 'loading' ? '<span class="sim-spinner"></span>' : '';
    el.innerHTML = `${spinner}<span>${msg}</span>`;
    el.dataset.mode = mode;
    el.style.display = 'flex';
}

function hideSimBanner() {
    const el = document.getElementById('simBanner');
    if (el) el.style.display = 'none';
}

async function loadSimFile() {
    try {
        const res = await apiFetch('data/tournament_sim.json?t=' + Date.now());
        if (!res.ok) return false;
        tournamentSim = await res.json();
        applySimData();
        showSyncWarnings(tournamentSim.syncInfo);
        const opt = document.getElementById('algTournament');
        if (opt) { opt.disabled = false; opt.title = ''; }
        // If the bracket tab is currently visible, refresh it so the user sees the
        // newly-loaded bracket instead of the "wird berechnet…" placeholder.
        const bracketTab = document.getElementById('bracketTab');
        if (bracketTab && bracketTab.classList.contains('active')) renderBracket();
        const versusTab = document.getElementById('versusTab');
        if (versusTab && versusTab.classList.contains('active')) renderVersus();
        return true;
    } catch (_) { return false; }
}

// ── Ambiguous-player picker modal ─────────────────────────────────────────────

function openAmbiguousPicker() {
    const info = tournamentSim?.syncInfo;
    if (!info?.ambiguous?.length) return;
    renderAmbiguousModal(info.ambiguous);
}

function closeAmbiguousPicker() {
    const m = document.getElementById('ambModal');
    if (m) m.remove();
}

function renderAmbiguousModal(ambiguous) {
    closeAmbiguousPicker();

    const modal = document.createElement('div');
    modal.id = 'ambModal';
    modal.className = 'amb-modal';
    modal.onclick = (e) => { if (e.target === modal) closeAmbiguousPicker(); };

    const sections = ambiguous.map(a => renderAmbSection(a)).join('');

    modal.innerHTML = `
        <div class="amb-dialog">
            <div class="amb-header">
                <div>
                    <h2 style="margin:0">Mehrdeutige Nachnamen</h2>
                    <p style="margin:0.3rem 0 0;font-size:0.85rem;color:var(--text-dim)">
                        Bracket nennt nur den Nachnamen — wähle den richtigen Spieler.
                    </p>
                </div>
                <button class="amb-close" onclick="closeAmbiguousPicker()" title="Schließen ohne speichern">×</button>
            </div>
            <div class="amb-body">${sections}</div>
            <div class="amb-footer">
                <button class="btn-inline btn-inline-ghost" onclick="closeAmbiguousPicker()">Abbrechen</button>
                <button class="btn-inline" onclick="confirmAmbiguousSelections()">💾 Speichern</button>
            </div>
        </div>`;
    document.body.appendChild(modal);
}

function confirmAmbiguousSelections() {
    const ambiguous = tournamentSim?.syncInfo?.ambiguous || [];
    const confirmed = getConfirmedAmbiguities();
    ambiguous.forEach(a => { confirmed[ambKey(a)] = true; });
    setConfirmedAmbiguities(confirmed);
    closeAmbiguousPicker();
    showSyncWarnings(tournamentSim.syncInfo);  // re-render banners (warning will now be hidden)
    showSimBanner('✓ Auswahl gespeichert', 'success');
    setTimeout(hideSimBanner, 2000);
}

function renderAmbSection(a) {
    const cards = a.candidates.map(c => {
        const sel = c.name === a.chosen ? ' amb-card-selected' : '';
        const avg = c.t > 0 ? (c.tp / c.t).toFixed(0) : '–';
        const pos = c.pos || '';
        const posCls = pos.toLowerCase();
        return `
        <div class="amb-card${sel}"
             data-from="${escapeAttr(a.chosen)}"
             data-to="${escapeAttr(c.name)}"
             data-lastname="${escapeAttr(a.lastName)}"
             onclick="pickAmbiguous(this)">
            ${c.img ? `<img src="${escapeAttr(c.img)}" alt="" class="amb-img"
                          onerror="this.style.display='none'"/>` : `<div class="amb-img-placeholder">${(c.name[0] || '?').toUpperCase()}</div>`}
            <div class="amb-card-body">
                <div class="amb-card-name">${escapeHtml(c.name)}</div>
                <div class="amb-card-meta">
                    <span class="cmp-pos cmp-pos-${posCls}">${pos[0] || '?'}</span>
                    <span class="amb-mini">${c.gender === 'M' ? '♂' : '♀'}</span>
                    <span class="amb-mini">${c.tp.toFixed(0)} Pts</span>
                    <span class="amb-mini">${c.t} T</span>
                    <span class="amb-mini">${avg} ⌀/T</span>
                </div>
            </div>
            ${sel ? '<div class="amb-check">✓</div>' : ''}
        </div>`;
    }).join('');

    const contextHint = a.teamContext && a.teamContext !== a.lastName
        ? ` <span class="amb-team-ctx">(Team: ${escapeHtml(a.teamContext)})</span>`
        : '';
    return `
    <div class="amb-section">
        <div class="amb-section-title">
            Bracket: <strong>"${escapeHtml(a.lastName)}"</strong>${contextHint}
        </div>
        <div class="amb-cards">${cards}</div>
    </div>`;
}

async function pickAmbiguous(el) {
    const from = el.dataset.from;
    const to   = el.dataset.to;
    if (from === to) return;  // already selected

    el.style.opacity = '0.5';
    try {
        const r = await apiFetch('/api/swap-player', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ from, to })
        });
        if (!r.ok) {
            const txt = await r.text();
            throw new Error(`HTTP ${r.status} — ${txt.slice(0, 120)}`);
        }
        // Reload player list (price preserved, name swapped)
        await loadPlayerData();
        // Update modal to reflect new selection
        await loadSimFile();
        if (tournamentSim?.syncInfo?.ambiguous) {
            renderAmbiguousModal(tournamentSim.syncInfo.ambiguous);
        }
        showSimBanner(`✓ ${from} ↔ ${to} getauscht`, 'success');
        setTimeout(hideSimBanner, 2500);
    } catch (err) {
        el.style.opacity = '1';
        if (String(err.message).includes('404')) {
            alert(
                'Endpoint /api/swap-player nicht gefunden.\n\n' +
                'Wahrscheinlich läuft eine alte Version von serve.py.\n' +
                'Bitte den Server neu starten:\n\n' +
                '  Strg+C im Terminal\n' +
                '  python scripts/serve.py'
            );
        } else {
            alert(`Fehler: ${err.message}`);
        }
    }
}

// ── Inline price-picker modal for entries with price ≤ 0 ─────────────────────

function openPricePicker() {
    const pending = (window.__playerDataMeta?.pending || []);
    if (!pending.length) return;

    // Look up each pending player's stats from allPlayers
    const norm = (s) => s.trim().toLowerCase().replace(/\s+/g, ' ');
    const byName = new Map(allPlayers.map(p => [norm(p.name), p]));
    const items = pending.map(name => byName.get(norm(name)) || { name, pos: '?', gender: '?', tp: 0, t: 0 });

    closePricePicker();

    const modal = document.createElement('div');
    modal.id = 'priceModal';
    modal.className = 'amb-modal';
    modal.onclick = (e) => { if (e.target === modal) closePricePicker(); };

    const rows = items.map((p, i) => {
        const posCls = (p.pos || '').toLowerCase();
        const avg = p.t > 0 ? (p.tp / p.t).toFixed(0) : '–';
        return `
        <div class="price-row">
            <div class="price-name">
                <div class="price-name-text">${escapeHtml(p.name || '')}</div>
                <div class="price-meta">
                    <span class="cmp-pos cmp-pos-${posCls}">${(p.pos || '?')[0]}</span>
                    <span class="amb-mini">${p.gender === 'M' ? '♂' : p.gender === 'W' ? '♀' : '?'}</span>
                    <span class="amb-mini">${p.tp.toFixed(0)} Pts</span>
                    <span class="amb-mini">${p.t} T</span>
                    <span class="amb-mini">${avg} ⌀/T</span>
                </div>
            </div>
            <div class="price-input-wrap">
                <input type="number" min="0" step="5" data-name="${escapeAttr(p.name)}"
                       class="price-input" placeholder="₡"
                       autocomplete="off"
                       ${i === 0 ? 'autofocus' : ''} />
            </div>
        </div>`;
    }).join('');

    modal.innerHTML = `
        <div class="amb-dialog">
            <div class="amb-header">
                <div>
                    <h2 style="margin:0">Preise eintragen</h2>
                    <p style="margin:0.3rem 0 0;font-size:0.85rem;color:var(--text-dim)">
                        Trage die Coin-Preise aus dem Fantasy-Board ein. Leere Felder werden übersprungen.
                    </p>
                </div>
                <button class="amb-close" onclick="closePricePicker()" title="Schließen">×</button>
            </div>
            <div class="amb-body">
                <div class="price-rows">${rows}</div>
            </div>
            <div class="amb-footer">
                <button class="btn-inline btn-inline-ghost" onclick="closePricePicker()">Abbrechen</button>
                <button class="btn-inline" onclick="savePrices()">💾 Speichern</button>
            </div>
        </div>`;
    document.body.appendChild(modal);

    // Allow Enter to submit
    modal.querySelectorAll('.price-input').forEach(inp => {
        inp.addEventListener('keydown', (e) => {
            if (e.key === 'Enter') { e.preventDefault(); savePrices(); }
        });
    });
    setTimeout(() => modal.querySelector('.price-input')?.focus(), 50);
}

function closePricePicker() {
    document.getElementById('priceModal')?.remove();
}

async function savePrices() {
    const inputs = document.querySelectorAll('#priceModal .price-input');
    const updates = [];
    inputs.forEach(inp => {
        const v = inp.value.trim();
        if (v === '') return;
        const num = parseInt(v, 10);
        if (isNaN(num) || num <= 0) return;
        updates.push({ name: inp.dataset.name, price: num });
    });

    if (updates.length === 0) {
        alert('Keine Preise eingegeben. Bitte mindestens einen Preis eintragen oder Abbrechen klicken.');
        return;
    }

    try {
        const r = await apiFetch('/api/set-prices', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ prices: updates }),
        });
        if (!r.ok) {
            const txt = await r.text();
            throw new Error(`HTTP ${r.status} — ${txt.slice(0, 120)}`);
        }
        closePricePicker();
        await loadPlayerData();   // reload to refresh availablePlayers + pending list
        showSimBanner(`✓ ${updates.length} Preise gespeichert`, 'success');
        setTimeout(hideSimBanner, 2500);
    } catch (err) {
        if (String(err.message).includes('404')) {
            alert(
                'Endpoint /api/set-prices nicht gefunden.\n\n' +
                'Bitte den Server neu starten:\n  Strg+C im Terminal\n  python scripts/serve.py'
            );
        } else {
            alert(`Fehler: ${err.message}`);
        }
    }
}

function escapeHtml(s) {
    return String(s).replace(/[&<>"]/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]));
}
function escapeAttr(s) { return escapeHtml(s); }

function showSyncWarnings(info) {
    // Sync warnings only make sense for admins — they're about Fantasy player
    // bookkeeping (price changes, unrecognised names) that elo accounts can't
    // act on anyway.
    if (!roleAtLeast(window.USER_ROLE, 'admin')) {
        const existing = document.getElementById('syncWarnings');
        if (existing) existing.remove();
        return;
    }
    const container = document.getElementById('syncWarnings') || (() => {
        const d = document.createElement('div');
        d.id = 'syncWarnings';
        document.querySelector('.container').insertBefore(d, document.querySelector('.controls'));
        return d;
    })();
    container.innerHTML = '';

    const warn = (msg, mode = 'warn') => {
        const el = document.createElement('div');
        el.className = 'sim-banner';
        el.dataset.mode = mode;
        el.innerHTML = msg;
        container.appendChild(el);
    };

    // ── Pending prices (price ≤ 0) ──
    // Offers Firestore-sync first (one-click, source-of-truth), then the
    // manual picker as a fallback if Firestore isn't set up.
    const meta = window.__playerDataMeta || {};
    if (meta.pending && meta.pending.length) {
        const banner = document.createElement('div');
        banner.className = 'sim-banner';
        banner.dataset.mode = 'warn';
        banner.innerHTML = `
            📝 <strong>${meta.pending.length} Preise fehlen</strong> für: ${meta.pending.join(', ')}
            <button class="btn-inline" onclick="refreshFirestorePrices()" style="margin-left:0.6rem"
                    title="Aus Firestore holen (einmaliges Setup: fetch_auth_token.txt)">
                📥 Aus Firestore
            </button>
            <button class="btn-inline btn-inline-ghost" onclick="openPricePicker()" style="margin-left:0.3rem">
                Manuell eintragen
            </button>`;
        container.appendChild(banner);
    }

    // ── Unknown names in players_available.json that don't match players_all ──
    if (meta.unknown && meta.unknown.length) {
        warn(`⚠ <strong>${meta.unknown.length} Spieler nicht erkannt</strong> in <code>players_available.json</code>: ${meta.unknown.join(', ')}`);
    }

    if (!info) return;

    if (info.unmatched && info.unmatched.length) {
        warn(`⚠ <strong>Bracket-Spieler fehlen in <code>players_all.json</code>:</strong> ${info.unmatched.join(', ')}<br>
              <span style="font-size:0.78rem;color:var(--text-dim)">→ Diese Spieler werden bei der Optimierung nicht berücksichtigt. Stammdaten ergänzen.</span>`);
    }
    if (info.ambiguous && info.ambiguous.length) {
        // Only show unconfirmed entries (user dismissed via Speichern button before)
        const confirmed = getConfirmedAmbiguities();
        const open = info.ambiguous.filter(a => !isAmbConfirmed(a, confirmed));
        if (open.length) {
            const banner = document.createElement('div');
            banner.className = 'sim-banner';
            banner.dataset.mode = 'warn';
            banner.innerHTML = `
                ⚠ <strong>${open.length} mehrdeutige Nachnamen</strong> — höchste Saison-Pts wurde gewählt.
                <button class="btn-inline" onclick="openAmbiguousPicker()" style="margin-left:0.6rem">
                    Auswahl prüfen
                </button>`;
            container.appendChild(banner);
        }
    }
    // (Entfernt-Liste wird absichtlich nicht angezeigt — zu viel Rauschen)
}

// localStorage helpers for ambiguous-warning dismissal
function getConfirmedAmbiguities() {
    try { return JSON.parse(localStorage.getItem('confirmedAmbiguities') || '{}'); }
    catch (_) { return {}; }
}
function setConfirmedAmbiguities(map) {
    localStorage.setItem('confirmedAmbiguities', JSON.stringify(map));
}
function ambKey(a) {
    // Key includes chosen name + sorted candidate ids → invalidates if either changes
    const ids = (a.candidates || []).map(c => c.id).sort().join(',');
    return `${a.lastName}|${a.chosen}|${ids}`;
}
function isAmbConfirmed(a, confirmed) {
    return confirmed[ambKey(a)] === true;
}

async function ensureTournamentSim() {
    // Check status — needs the Python server (serve.py). If not running, fall back to file-only mode.
    let status = null;
    try {
        const r = await apiFetch('/api/sim-status');
        if (r.ok) status = await r.json();
    } catch (_) { /* serve.py not running */ }

    if (!status) {
        // No backend — try to load existing file silently
        const ok = await loadSimFile();
        if (!ok) {
            showSimBanner(
                '⚠ Turnier-Prognose nicht verfügbar. Starte <code>python scripts/serve.py</code> für Auto-Update.',
                'warn'
            );
        }
        return;
    }

    if (status.exists && status.fresh) {
        await loadSimFile();
        return;
    }

    const reason = !status.exists ? 'noch nicht berechnet' :
                   !status.playersHashMatch ? 'Spielerliste hat sich geändert' :
                   'Daten älter als 6 h';
    await waitForSim(reason);
}

// Trigger sim and poll status until fresh. Survives 409 (another tab is running it),
// network timeouts on the long POST, and partial file writes. Updates the banner with
// a live seconds counter so the user can see progress instead of an instant error.
async function waitForSim(reason) {
    const startedAt = Date.now();
    const POLL_INTERVAL_MS = 3000;
    const TIMEOUT_MS = 4 * 60 * 1000;   // 4 min — sim should finish in well under 2

    const tickBanner = () => {
        const secs = Math.floor((Date.now() - startedAt) / 1000);
        showSimBanner(`Turnier-Prognose wird berechnet (${reason}) … ${secs}s`, 'loading');
    };
    tickBanner();
    const bannerTimer = setInterval(tickBanner, 1000);

    // Kick off the sim. We don't await this — the server may take longer than the
    // browser's idle timeout, and a 409 just means another tab already started one.
    // We rely on the polling loop below to detect completion.
    apiFetch('/api/simulate?gender=all', { method: 'POST' }).catch(() => {});

    try {
        while (Date.now() - startedAt < TIMEOUT_MS) {
            await new Promise(r => setTimeout(r, POLL_INTERVAL_MS));
            let status = null;
            try {
                const r = await apiFetch('/api/sim-status');
                if (r.ok) status = await r.json();
            } catch (_) { /* transient network blip — keep polling */ }
            if (status && status.exists && status.fresh) {
                const ok = await loadSimFile();
                if (ok) {
                    const dur = Math.round((Date.now() - startedAt) / 1000);
                    showSimBanner(`✓ Prognose aktualisiert (${dur}s)`, 'success');
                    setTimeout(hideSimBanner, 3000);
                    return;
                }
            }
        }
        showSimBanner(
            '⚠ Berechnung dauert ungewöhnlich lange. <a href="javascript:location.reload()">Seite neu laden</a>.',
            'warn'
        );
    } finally {
        clearInterval(bannerTimer);
    }
}

async function refreshSim() {
    // First make sure the API server is actually running
    try {
        const status = await apiFetch('/api/sim-status');
        if (!status.ok) throw new Error('no api');
    } catch (_) {
        showSimBanner(
            '⚠ Auto-Update nicht verfügbar. Starte <code>python scripts/serve.py</code> ' +
            'statt <code>python -m http.server</code>.',
            'warn'
        );
        return;
    }

    showSimBanner('Prognose wird neu berechnet (lädt DVV / Bracket / H2H neu)…', 'loading');
    try {
        const r = await apiFetch('/api/simulate?gender=all&force=1', { method: 'POST' });
        if (!r.ok) {
            const txt = await r.text();
            throw new Error(`HTTP ${r.status} — ${txt.slice(0, 120)}`);
        }
        const result = await r.json();
        // Auto-sync may have rewritten players_available.json (added/removed players)
        await loadPlayerData();
        await loadSimFile();
        showSimBanner(`✓ Prognose neu berechnet (${result.duration_s}s)`, 'success');
        setTimeout(hideSimBanner, 3000);
    } catch (err) {
        showSimBanner(`Fehler: ${err.message}`, 'warn');
    }
}

// ── Firestore-Sync: holt Preise + Spielerliste aus dem gbt-fantasy.web.app
// Firestore-Doc direkt, ohne dass der User Preise eintippen muss.
// Setup-Anleitung in `fetch_auth_token.txt`.
async function refreshFirestorePrices() {
    try {
        const status = await apiFetch('/api/sim-status');
        if (!status.ok) throw new Error('no api');
    } catch (_) {
        showSimBanner('⚠ Server nicht erreichbar — bitte <code>python scripts/serve.py</code> starten.', 'warn');
        return;
    }

    showSimBanner('Lade Preise aus Firestore…', 'loading');
    try {
        const r = await apiFetch('/api/firestore-sync?force=1', { method: 'POST' });
        const result = await r.json().catch(() => ({}));
        if (!r.ok) {
            // 401 = no auth setup yet — friendlier guidance, with a link to the snippet file.
            if (r.status === 401) {
                showSimBanner(
                    `⚠ ${result.error || 'Firestore-Auth fehlt'}. ` +
                    `Setup: <code>fetch_auth_token.txt</code> in der DevTools-Konsole von gbt-fantasy.web.app ausführen.`,
                    'warn'
                );
                return;
            }
            throw new Error(`HTTP ${r.status} — ${result.error || ''}`);
        }
        // Pull the fresh player list so the new prices show up immediately.
        await loadPlayerData();
        const changes = result.prices_changed || [];
        const msg = changes.length
            ? `✓ Firestore-Sync: ${result.players_in_snapshot} Spieler · ${changes.length} Preise geändert`
            : `✓ Firestore-Sync: ${result.players_in_snapshot} Spieler · Preise unverändert`;
        showSimBanner(msg, 'success');
        setTimeout(hideSimBanner, 4000);
    } catch (err) {
        showSimBanner(`Firestore-Fehler: ${err.message}`, 'warn');
    }
}

// Apply expected match data from simulation to available players
function applySimData() {
    if (!tournamentSim) return;
    const em = tournamentSim.playerExpectedMatches || {};
    availablePlayers.forEach(p => {
        p.expectedMatches   = em[p.id] ?? null;
        // Expected points this tournament: avgPerMatch * expected matches
        p.expectedPoints    = (p.expectedMatches !== null && p.avgPerMatch > 0)
            ? p.avgPerMatch * p.expectedMatches
            : null;
        p.expectedPerCoin   = (p.expectedPoints !== null && p.price > 0)
            ? p.expectedPoints / p.price
            : null;
    });
}

function buildPlayer(raw, price) {
    return {
        id:               raw.id,
        name:             raw.firstName + ' ' + raw.lastName,
        firstName:        raw.firstName,
        lastName:         raw.lastName,
        pos:              raw.pos,
        gender:           raw.gender,
        tp:               raw.tp,
        t:                raw.t,
        mp:               raw.mp,
        img:              raw.img || null,
        price,
        available:        price !== null,
        avgPerTournament: raw.t  > 0 ? raw.tp / raw.t  : 0,
        avgPerMatch:      raw.mp > 0 ? raw.tp / raw.mp : 0,
        // pts per coin = avg points per tournament / price (team is picked for ONE tournament)
        avgPerCoin:       (price > 0 && raw.t > 0) ? (raw.tp / raw.t) / price : 0,
    };
}

// For players with no recorded statistics, estimate stats from peers at the
// same position and a similar price (±5 coins). Applied with a 0.75 discount
// to signal lower confidence. Only fires when t === 0 AND tp === 0.
function computePoolEstimates(players) {
    const withStats = players.filter(p => p.t > 0 && p.tp > 0);
    players.forEach(p => {
        if (p.t > 0 || p.tp > 0) return;
        if (p.price === null || p.price <= 0) return;
        const peers = withStats.filter(q =>
            q.pos === p.pos &&
            Math.abs(q.price - p.price) <= 5 &&
            q.id !== p.id
        );
        if (peers.length === 0) return;
        const avgPT = peers.reduce((s, q) => s + q.avgPerTournament, 0) / peers.length;
        const avgPM = peers.reduce((s, q) => s + q.avgPerMatch,      0) / peers.length;
        p.avgPerTournament = avgPT * 0.75;
        p.avgPerMatch      = avgPM * 0.75;
        p.tp               = p.avgPerTournament;  // pretend 1 tournament
        p.t                = 1;
        p.avgPerCoin       = p.price > 0 ? p.avgPerTournament / p.price : 0;
        p.isEstimated      = true;
    });
}

// ── Tab navigation ────────────────────────────────────────────────────────────

function switchTab(tab, el) {
    document.querySelectorAll('.tab').forEach(t => t.classList.remove('active'));
    document.querySelectorAll('.tab-content').forEach(c => c.classList.remove('active'));

    el.classList.add('active');
    document.getElementById(tab + 'Tab').classList.add('active');

    if (tab === 'players')      renderPlayers();
    else if (tab === 'picks')   renderPicksTab();
    else if (tab === 'compare') renderCompare();
    else if (tab === 'bracket') renderBracket();
    else if (tab === 'versus')  renderVersus();
    else if (tab === 'elo')     renderEloRanking();
    else if (tab === 'elotune') renderEloTuning();
}

// ── ELO ranking tab ──────────────────────────────────────────────────────────
//
// Reads data/elo_current.json (produced by `python scripts/elo/build_ratings.py
// --phase build`). Standalone from the rest of the algorithm pipeline — this is
// an inspection view, not yet wired into team picking.

const _eloDataByModel = {};     // model_id → parsed JSON cache
let _eloMeta = null;            // comparison meta for the stats line
let _eloCurrentModel = 'elo';
let _eloFilters = {
    country: 'all',             // 'all' | 'germany' | 'intl'
    gender:  'all',             // 'all' | 'm' | 'f'
    search:  '',
    activeOnly: true,
};

async function renderEloRanking() {
    const host = document.getElementById('eloRanking');
    if (!host) return;

    // First entry into the tab: wire the filter/model-select handlers once
    if (!_eloFiltersWired) {
        _wireEloFilters();
        _eloFiltersWired = true;
    }
    // Preload meta for the comparison line (best-effort)
    if (_eloMeta === null) {
        try {
            const r = await apiFetch('data/elo_models_meta.json?t=' + Date.now());
            _eloMeta = r.ok ? await r.json() : { models: [] };
        } catch { _eloMeta = { models: [] }; }
        _updateEloModelStats();
    }
    _maybeShowStaleBanner(_eloMeta);
    // Load the currently-selected model's player list (with cache per model)
    if (!_eloDataByModel[_eloCurrentModel]) {
        try {
            const r = await apiFetch(`data/${_eloCurrentModel}_current.json?t=` + Date.now());
            if (!r.ok) throw new Error(r.status + ' ' + r.statusText);
            _eloDataByModel[_eloCurrentModel] = await r.json();
        } catch (e) {
            host.innerHTML = `<div class="no-results">
                ${_eloCurrentModel.toUpperCase()}-Daten nicht verfügbar (${e.message}).<br><br>
                Einmal laufen lassen:<br>
                <code>python scripts/elo/build_ratings.py --phase build</code>
            </div>`;
            return;
        }
    }
    _drawEloRanking();
}

let _eloFiltersWired = false;

// ── Info modal + model/slider explanations ──────────────────────────────────

const MODEL_INFO = {
    elo: {
        title: 'ELO (klassisch)',
        simple: `
            <p>Das berühmteste Rating-System der Welt — ursprünglich für Schach (1960er), heute überall: FIFA, Chess.com, sogar Tinder.</p>
            <p><strong>Grundidee:</strong> Jeder Spieler hat eine Zahl (typisch um 1500). Bei einem Sieg gegen einen stärkeren Gegner steigt die Zahl deutlich, bei einem Sieg gegen einen schwächeren nur leicht. Bei einer Niederlage umgekehrt.</p>
            <p>Wir haben dem System ein paar Beach-spezifische Anpassungen verpasst: ein deutlicher Sieg (21:5) zählt mehr als ein knapper (21:19), Finale wiegen schwerer als Qualifikations-Spiele, und neue Teams nutzen die Stärke ihrer Einzelspieler bis sie selbst genug Historie haben.</p>
            <p><strong>Wie genau?</strong> Sagt 66,8 % aller bisher ungesehenen Matches richtig vorher.</p>
        `,
        tech: `
            <p>Klassisches Elo nach Arpad Elo, mit Beach-spezifischen Erweiterungen:</p>
            <ul>
                <li><strong>Margin-of-Victory:</strong> Set-Score-Differenz als K-Multiplikator (Stärke einstellbar via <code>mov_strength</code>).</li>
                <li><strong>Importance-Weights:</strong> Quali / Hauptrunde / Finals haben unterschiedliche Multiplikatoren (<code>importance_quali</code>, <code>importance_final</code>).</li>
                <li><strong>Source-Weights:</strong> DVV / FIVB / bvbinfo getrennt gewichtbar (<code>source_weight_*</code>).</li>
                <li><strong>Tier-Weights:</strong> Innerhalb DVV können Challenger / Qualifier-only-Turniere herunter-gewichtet werden.</li>
                <li><strong>Blend Einzel/Team:</strong> Team-Rating = <code>blend_individual_weight</code> · μ(Einzel) + (1−w) · μ(Team-Historie). Team-Blend erst ab <code>team_min_matches_for_blend</code>.</li>
                <li><strong>Provisional-K:</strong> Spieler &lt; 10 Matches bekommen K × <code>provisional_multiplier</code>.</li>
                <li><strong>Decay:</strong> Pro Jahr Inaktivität wird das Rating mit Faktor <code>decay_pull</code> Richtung 1500 gezogen.</li>
            </ul>
            <p>Persistiert Match-Predictions + History in <code>data/elo_ratings.db</code> (SQLite). OOS (n=5646, Train ≤2024-12-31): <strong>Accuracy 66,8 %</strong>, Calibration-Error 0,013. Grid-tuned defaults: <code>k_base=30, blend=0.8</code>.</p>
        `,
    },
    glicko2: {
        title: 'Glicko-2',
        simple: `
            <p>Elo's klügerer Cousin (Mark Glickman, 2013). Das System merkt sich nicht nur, <strong>wie gut</strong> jemand ist, sondern auch <strong>wie sicher es sich da ist</strong>.</p>
            <p>Wenn jemand lange nicht gespielt hat, sagt das System "ich weiß gerade nicht so genau wo er steht" und reagiert dann stärker auf das nächste Spiel. Zusätzlich erkennt es, ob jemand gerade stabil spielt (Routinier) oder sprunghaft (junges Talent, formschwacher Star).</p>
            <p>Bei knappen Vorhersagen — z.B. wenn unklar ist, ob jemand 60 % oder 70 % Chance hat — ist Glicko-2 ehrlicher als Elo. Es behauptet seltener Sicherheit, wo keine ist.</p>
            <p><strong>Wie genau?</strong> 66,3 % korrekte Vorhersagen, dafür mit Abstand bester Kalibrierungsfehler (0,008) — die berechneten Wahrscheinlichkeiten stimmen.</p>
        `,
        tech: `
            <p>Glicko-2 nach Glickman (2013). Erweitert Elo um zwei Bayes-Größen pro Spieler:</p>
            <ul>
                <li><strong>φ (Rating Deviation):</strong> Unsicherheits-Maß. Wächst bei Inaktivität, schrumpft bei jedem Match. Start <code>initial_phi=200</code> (Original: 350; wir starten mit niedrigerem φ weil DVV-Priors angewendet werden).</li>
                <li><strong>σ (Volatility):</strong> Wie sprunghaft sich das Skill-Level ändern darf. Constraint via <code>tau</code> (Original 0.5, wir: 0.3 für stabilere Ratings).</li>
                <li><strong>Rating Periods:</strong> Matches innerhalb eines <code>rating_period_days</code>-Fensters werden als ein Bayes-Update verarbeitet — verbessert die Schätzung bei mehreren Spielen am gleichen Tag.</li>
                <li><strong>Team-Rating:</strong> μ-Mittel der Einzel-Spieler; φ ist die quadrierte Summe (Var = Σ var_i).</li>
            </ul>
            <p>Konvertierung Glicko-2 ↔ Elo-Scale: <code>μ_g2 × 173.7178 + 1500</code>. Kein MoV-Term implementiert. OOS: <strong>Accuracy 66,3 %, CE 0,008</strong> — bestes Einzelmodell nach Kalibrierung.</p>
        `,
    },
    trueskill: {
        title: 'TrueSkill (Microsoft Halo)',
        simple: `
            <p>Microsoft hat TrueSkill für Xbox Live entwickelt, ursprünglich für Halo: 4-vs-4-Matchmaking auf Sekunden-Basis. Das System ist also auf <strong>Team-Spiele und Matchmaking</strong> ausgelegt — passt perfekt für Beach (2 vs 2).</p>
            <p>Wie Glicko-2 kennt es nicht nur die Stärke, sondern auch die Unsicherheit. Außerdem behandelt es das Team als Ganzes (statt nur die einzelnen Spieler) und liefert direkt eine Antwort auf "wie wahrscheinlich gewinnt Team A".</p>
            <p>Bei uns liefert es die <strong>höchste Accuracy</strong> (gemeinsam mit Elo: 66,8 %), ist aber etwas zu "selbstbewusst" — sagt manchmal 80 % wo nur 70 % gerechtfertigt wären. Trotzdem wertvoll als Komponente im Ensemble.</p>
        `,
        tech: `
            <p>TrueSkill (Herbrich/Minka/Graepel 2007), Implementierung via <code>trueskill</code> PyPI-Package. Faktor-Graph mit Gauß-Belief-Propagation.</p>
            <ul>
                <li><strong>μ (Skill-Schätzung):</strong> Mittel der Skill-Posterior. Start <code>initial_mu=25</code>, "exposed Skill" = μ − 3σ (= 99,7 %-Konfidenz-Untergrenze).</li>
                <li><strong>σ (Skill-Unsicherheit):</strong> Std. Start <code>initial_sigma_ts=5.0</code> bei uns (Halo: 8.33 = μ/3) — wir starten enger, weil DVV-Priors injiziert werden.</li>
                <li><strong>β:</strong> Performance-Variance. Default 6.0 bei uns (Halo: μ/6 ≈ 4.17). Größeres β = weniger Skill-Distance nötig für deterministisches Ergebnis.</li>
                <li><strong>τ:</strong> Pro-Match σ-Drift. Default 0.02 (Halo: 0.083 ≈ μ/300) — wir sind stabiler weil Beach langsamere Skill-Dynamik hat als Shooter.</li>
                <li><strong>Team-Rating:</strong> Summe der Spieler-μ (NICHT Mittel). 2v2 nativ — Spieler-Updates fallen aus dem TrueSkill-Solver direkt raus.</li>
            </ul>
            <p>Display-Skala normalisiert über <code>display_offset()</code> auf Elo-1500. Kein MoV. OOS: <strong>Accuracy 66,8 %, CE 0,032</strong>.</p>
        `,
    },
    ensemble: {
        title: 'Ensemble (1:1:1 Mittel)',
        simple: `
            <p>Warum sich für ein Modell entscheiden, wenn man alle drei nutzen kann?</p>
            <p>Bei jedem Match werden parallel alle drei Modelle aktualisiert (ELO + Glicko-2 + TrueSkill). Die endgültige Vorhersage ist der Durchschnitt der drei. Wenn alle drei zustimmen ("70 % Sieg") ist das ein starkes Signal. Wenn zwei sagen 60 % und einer 80 %, glätten wir das auf einen vernünftigen Mittelwert.</p>
            <p>Resultat: <strong>67,1 % Accuracy</strong> (besser als jedes Einzelmodell) und gleichzeitig <strong>der niedrigste Kalibrierungsfehler</strong>. Das beste Modell für die Rangliste.</p>
            <p><em>Standardauswahl — wenn du nicht extra vergleichen willst, bleib hier.</em></p>
        `,
        tech: `
            <p>Holds three child models internally, predicts as weighted Mittel:</p>
            <ul>
                <li><code>weight_elo</code> · P_elo + <code>weight_glicko2</code> · P_glicko2 + <code>weight_trueskill</code> · P_trueskill (Standard 1:1:1).</li>
                <li>Display-Ratings normalisiert über <code>_normalise_to_elo</code> auf die 1500-Skala.</li>
                <li>State-Dicts (<code>state_indiv</code>, <code>state_team</code>) sind plain attribute dicts mit key-only mirrors — werden incrementell pro Match aktualisiert, NIEMALS on-access re-derived (würde O(N²) im Runner verursachen, 1h+ build-hang).</li>
                <li>Per-match <code>pre_team</code>-Aggregation ruft <code>display_offset()</code> jedes Sub-Modells auf → in heißen Loops potenziell teuer (TrueSkill instanziiert dabei ein Rating-Objekt).</li>
            </ul>
            <p>Kein eigenes K oder φ — Gewichte sind die einzigen tunable Hyperparameter. OOS: <strong>Accuracy 67,1 %, CE 0,007</strong> — beide Metriken bestes Resultat im Vergleich, schlägt jedes Einzelmodell.</p>
        `,
    },
};

const MODEL_INFO_MODES = ['simple', 'tech'];

const SLIDER_INFO = {
    // Elo
    k_base: 'Wie stark sich das Rating pro Match verändert. Größer = reaktiver, schwankender. Kleiner = stabiler, träger. Standard 30 (grid-tuned).',
    blend_individual_weight: 'Anteil des Einzel-Spieler-Ratings im Team-Rating. Rest ist Team-Historie. 1.0 = nur Einzel, 0.5 = halb-halb. Standard 0.8 — Einzel-Skill ist meistens das stärkere Signal.',
    decay_pull: 'Jährlicher Decay-Faktor: ungespielt-Spieler driften pro Jahr um diesen Anteil Richtung 1500 (Mittelmaß) zurück. 0.10 = 10 % pro Jahr.',
    team_min_matches_for_blend: 'Erst ab so vielen Team-Matches wird die Team-Historie in den Blend reingezogen. Frische Teams = nur Einzel-Ratings nutzen.',
    provisional_multiplier: 'Neue Spieler (< 10 Matches) bekommen einen K-Multiplikator. 2.0 = doppelt so reaktiv, damit unbekannte Skill-Levels schnell gefunden werden.',
    importance_quali: 'Gewicht für Qualifikations-Spiele. < 1.0 = zählen weniger (oft niedrigeres Niveau, weniger Aussagekraft).',
    importance_final: 'Gewicht für Final-Runden (HF/F). > 1.0 = zählen mehr (Top-Teams unter Druck — aussagekräftiger).',
    mov_strength: 'Margin-of-Victory-Stärke: wie viel der Punktdifferenz im Match das K skaliert. 0 = MoV aus, 1.0 = voll. Standard 1.0.',
    source_weight_dvv: 'Gewicht aller DVV-Matches (German Beach Tour). 1.0 = volles Gewicht.',
    source_weight_fivb: 'Gewicht aller FIVB-Matches (World Tour, Olympia, World Championships). 1.0 = volles Gewicht.',
    source_weight_bvb: 'Gewicht aller bvbinfo-Matches (AVP und alle anderen US- + internationale Touren). 1.0 = volles Gewicht.',
    tier_weight_challenger: 'Innerhalb DVV: zusätzlicher Multiplikator für reine Challenger-Format-Turniere (kein Open-Niveau). Standard 0.5 = halbes Gewicht.',
    tier_weight_qualifier: 'Innerhalb DVV: zusätzlicher Multiplikator für Qualifier-only-Turniere. Standard 0.3.',
    // Glicko-2
    initial_phi: 'Start-φ (Unsicherheit) für neue Spieler. Hoch = mehr Bewegung in den ersten Matches. Standard 200 (Original-Glicko: 350).',
    initial_sigma: 'Start-σ (Volatilität). Wie sprunghaft sich das Skill-Level eines neuen Spielers ändern darf.',
    tau: 'System-Volatilitäts-Constraint. Klein (0.3) = volatility ändert sich nur langsam; größer = reaktiver auf Ergebnis-Streuung.',
    rating_period_days: 'Wie lange ein Rating-Period geht. Default 7 Tage: alle Matches einer Woche werden zusammen berechnet (= gemeinsame Bayes-Update).',
    // TrueSkill
    initial_mu: 'Start-μ (Skill-Schätzung) für neue Spieler. Standard 25 (Halo-Default).',
    initial_sigma_ts: 'Start-σ (Unsicherheit). Standard 5.0 bei uns (Halo: 8.33) — Beach-Spieler kommen oft mit etwas Vorwissen.',
    beta: 'Skill-Distance für sichere Siege. Höher = mehr Skill-Unterschied nötig für deterministisches Ergebnis. Standard 6.0.',
    tau_ts: 'Zeit-Drift pro Match. Klein = Ratings frieren langsam ein; größer = altes Wissen verfällt schneller.',
    draw_probability: 'Wahrscheinlichkeit für Unentschieden. Bei Beach ~0 (nur in Pool-Phase möglich).',
    // Ensemble
    weight_elo: 'Gewicht des Elo-Sub-Modells im finalen Mittel.',
    weight_glicko2: 'Gewicht des Glicko-2-Sub-Modells im finalen Mittel.',
    weight_trueskill: 'Gewicht des TrueSkill-Sub-Modells im finalen Mittel.',
};

function openInfoModal(title, html) {
    const old = document.getElementById('infoModalBackdrop');
    if (old) old.remove();
    const bd = document.createElement('div');
    bd.id = 'infoModalBackdrop';
    bd.className = 'info-modal-backdrop';
    bd.innerHTML = `
        <div class="info-modal" onclick="event.stopPropagation()">
            <button class="info-modal-close" onclick="document.getElementById('infoModalBackdrop').remove()" title="Schließen">×</button>
            <h3>${title}</h3>
            ${html}
        </div>`;
    bd.addEventListener('click', () => bd.remove());
    bd.addEventListener('keydown', (e) => { if (e.key === 'Escape') bd.remove(); });
    document.body.appendChild(bd);
}

function showModelInfo(modelId, mode = 'simple') {
    const info = MODEL_INFO[modelId];
    if (!info) return;
    const body = info[mode] || info.simple;
    openInfoModal(info.title, body);
}

function showSliderInfo(key) {
    const spec = (_elotuneSchema?.sliders || []).find(s => s.key === key);
    const label = spec?.label || key;
    const desc = SLIDER_INFO[key] || 'Keine Beschreibung hinterlegt.';
    const range = spec ? `<p><small>Bereich: <code>${spec.min}</code> – <code>${spec.max}</code> · Default: <code>${spec.default}</code></small></p>` : '';
    openInfoModal(label, `<p>${desc}</p>${range}<p><small>Slider-Key: <code>${key}</code></small></p>`);
}

// Show a "Daten X h alt — Neu berechnen" banner when elo_models_meta.json's
// generated_at is older than 24h. Only visible for elo_lab+ (viewer can't act
// on it). Doesn't touch #eloRefreshBanner (reserved for active-refresh state).
function _maybeShowStaleBanner(meta) {
    const el = document.getElementById('eloStaleBanner');
    if (!el) return;
    // Cloud mode never computes ELO — the "neu berechnen" action is local-only,
    // so a staleness prompt is meaningless there. Ratings are refreshed by the
    // maintainer via scripts/elo/publish.py.
    if (window.SUPABASE_URL) { el.hidden = true; return; }
    if (!roleAtLeast(window.USER_ROLE, 'elo_lab')) { el.hidden = true; return; }
    const ts = meta && meta.generated_at;
    if (!ts) { el.hidden = true; return; }
    const ageMs = Date.now() - new Date(ts).getTime();
    if (!isFinite(ageMs) || ageMs < 24 * 3600 * 1000) { el.hidden = true; return; }
    const hours = Math.round(ageMs / 3600 / 1000);
    el.innerHTML = `⚠ ELO-Daten sind ${hours} h alt. `
        + `<button onclick="_eloTriggerRefresh()">🔄 Jetzt neu berechnen</button>`;
    el.hidden = false;
}

function _updateEloModelStats() {
    const el = document.getElementById('eloModelStats');
    if (!el || !_eloMeta || !_eloMeta.models) return;
    el.innerHTML = _eloMeta.models.map(m => {
        const acc = m.oos_acc != null ? (m.oos_acc * 100).toFixed(1) + '%' : '–';
        const cal = m.oos_calib != null ? m.oos_calib.toFixed(3) : '–';
        const cls = m.id === _eloCurrentModel ? 'active' : '';
        return `<span class="${cls}" style="margin-left:0.8rem">${m.id}: OOS ${acc} · calib ${cal}</span>`;
    }).join('');
}

function _wireEloFilters() {
    const sel = document.getElementById('eloModelSelect');
    if (sel) {
        sel.value = _eloCurrentModel;
        sel.addEventListener('change', () => {
            _eloCurrentModel = sel.value;
            _updateEloModelStats();
            renderEloRanking();
        });
    }
    document.querySelectorAll('#eloCountryFilter .filter-pill').forEach(btn => {
        btn.addEventListener('click', () => {
            document.querySelectorAll('#eloCountryFilter .filter-pill')
                .forEach(b => b.classList.remove('active'));
            btn.classList.add('active');
            _eloFilters.country = btn.dataset.country;
            _drawEloRanking();
        });
    });
    document.querySelectorAll('#eloGenderFilter .filter-pill').forEach(btn => {
        btn.addEventListener('click', () => {
            document.querySelectorAll('#eloGenderFilter .filter-pill')
                .forEach(b => b.classList.remove('active'));
            btn.classList.add('active');
            _eloFilters.gender = btn.dataset.gender;
            _drawEloRanking();
        });
    });
    const search = document.getElementById('eloSearch');
    if (search) {
        search.addEventListener('input', e => {
            _eloFilters.search = e.target.value.trim().toLowerCase();
            _drawEloRanking();
        });
    }
    const active = document.getElementById('eloActiveOnly');
    if (active) {
        active.addEventListener('change', e => {
            _eloFilters.activeOnly = e.target.checked;
            _drawEloRanking();
        });
    }
    const refreshBtn = document.getElementById('eloRefreshBtn');
    if (refreshBtn) {
        refreshBtn.addEventListener('click', () => _eloTriggerRefresh());
    }
}

// ── Smart ELO refresh: fires backend job + polls status ─────────────────────
let _eloRefreshPolling = false;

function _showEloRefreshBanner(text, kind, withProgress = false) {
    const el = document.getElementById('eloRefreshBanner');
    if (!el) return;
    el.hidden = false;
    el.className = 'elo-refresh-banner' + (kind ? ' ' + kind : '');
    el.innerHTML = text + (withProgress ? '<div class="progress-indeterminate" style="margin:0.4rem 0 0 0"></div>' : '');
}

async function _eloTriggerRefresh() {
    if (_eloRefreshPolling) return;
    const cloud = !!(window.API_BASE);
    const dur = cloud
        ? '<strong>Dauert in der Cloud mehrere Minuten</strong> '
          + '(~6 min bei keinen neuen Matches, bis ~30 min für einen Full-Rebuild). '
          + 'Du kannst die Seite verlassen — beim nächsten Besuch sind die Daten frisch.'
        : 'Dauert lokal ~30 s, bei Full-Rebuild bis 10 min.';
    if (!confirm('🔄 Update der ELO-Daten starten?\n\n' + dur.replace(/<[^>]+>/g, ''))) {
        return;
    }
    const btn = document.getElementById('eloRefreshBtn');
    if (btn) btn.disabled = true;
    _showEloRefreshBanner('⏳ Starte Update-Check… ' + dur, '', true);
    try {
        const r = await apiFetch('/api/elo-refresh', {method: 'POST'});
        if (!r.ok && r.status !== 202 && r.status !== 409) {
            const j = await r.json().catch(() => ({}));
            _showEloRefreshBanner('❌ ' + (j.error || ('HTTP ' + r.status)), 'error');
            if (btn) btn.disabled = false;
            return;
        }
        // 202 = started, 409 = already running → both are fine, poll
    } catch (e) {
        _showEloRefreshBanner('❌ Netzwerkfehler: ' + e.message, 'error');
        if (btn) btn.disabled = false;
        return;
    }
    _eloRefreshPolling = true;
    _eloPollRefresh();
}

async function _eloPollRefresh() {
    try {
        const r = await apiFetch('/api/elo-refresh-status');
        const s = await r.json();
        if (s.phase === 'done') {
            const sum = s.summary || {};
            const delta = (sum.matches_after ?? 0) - (sum.matches_before ?? 0);
            if (sum.rebuilt) {
                _showEloRefreshBanner(
                    `✅ ${delta > 0 ? '+' : ''}${delta} neue Matches · Modelle neu gebaut`,
                    'ok'
                );
                // Refresh in-page data
                Object.keys(_eloDataByModel).forEach(k => delete _eloDataByModel[k]);
                _eloMeta = null;
                renderEloRanking();
            } else {
                _showEloRefreshBanner(
                    `✅ Datenstand aktuell (${sum.matches_after?.toLocaleString() ?? '?'} Matches, kein Rebuild nötig)`,
                    'ok'
                );
            }
            _eloRefreshDone();
            return;
        }
        if (s.phase === 'error') {
            _showEloRefreshBanner('❌ ' + (s.message || 'unbekannter Fehler'), 'error');
            _eloRefreshDone();
            return;
        }
        const phaseEmoji = {
            discovering: '🔍', fetching: '⬇️',
            checking: '🔎', building: '🛠️',
        }[s.phase] || '⏳';
        const elapsed = s.started_at
            ? Math.floor((Date.now() - new Date(s.started_at).getTime()) / 1000)
            : null;
        const elapsedTxt = elapsed != null ? ` · ${elapsed}s` : '';
        _showEloRefreshBanner(`${phaseEmoji} ${s.message || s.phase}…${elapsedTxt}`, '', true);
        setTimeout(_eloPollRefresh, 3000);
    } catch (e) {
        _showEloRefreshBanner('⚠ Polling-Fehler: ' + e.message, 'error');
        _eloRefreshDone();
    }
}

function _eloRefreshDone() {
    _eloRefreshPolling = false;
    const btn = document.getElementById('eloRefreshBtn');
    if (btn) btn.disabled = false;
}

function _normaliseStr(s) {
    return (s || '').toLowerCase().normalize('NFKD')
        .replace(/[̀-ͯ]/g, '');
}

function _drawEloRanking() {
    const host = document.getElementById('eloRanking');
    const data = _eloDataByModel[_eloCurrentModel];
    if (!host || !data) return;
    const players = data.players || [];

    // 2-year activity cutoff for "active" filter
    const cutoffDate = (() => {
        const d = new Date();
        d.setFullYear(d.getFullYear() - 2);
        return d.toISOString().slice(0, 10);
    })();

    const search = _normaliseStr(_eloFilters.search);
    const filtered = players.filter(p => {
        if (_eloFilters.gender !== 'all' && p.gender !== _eloFilters.gender) return false;
        if (_eloFilters.country === 'germany'
                && _normaliseStr(p.country) !== 'germany') return false;
        if (_eloFilters.country === 'intl'
                && _normaliseStr(p.country) === 'germany') return false;
        if (_eloFilters.activeOnly) {
            if ((p.matches || 0) < 5) return false;
            if (!p.last_active || p.last_active < cutoffDate) return false;
        }
        if (search) {
            const hay = _normaliseStr(p.name) + ' ' + _normaliseStr(p.country);
            if (!hay.includes(search)) return false;
        }
        return true;
    });

    filtered.sort((a, b) => (b.elo_combined || b.elo_individual)
                          - (a.elo_combined || a.elo_individual));

    if (!filtered.length) {
        host.innerHTML = `<div class="elo-summary">0 / ${players.length} Spieler</div>
            <div class="no-results">Keine Treffer für diese Filter.</div>`;
        return;
    }

    const summary = `<div class="elo-summary">
        ${filtered.length.toLocaleString('de-DE')} / ${players.length.toLocaleString('de-DE')} Spieler
    </div>`;

    const visible = filtered.slice(0, 500);   // safety cap on DOM nodes
    const rows = visible.map((p, i) => `
        <tr>
            <td class="rank num">${i + 1}</td>
            <td class="gender-cell">${p.gender === 'm' ? '♂' : p.gender === 'f' ? '♀' : ''}</td>
            <td>${_escapeHtml(p.name)}</td>
            <td class="country-cell">${_escapeHtml(p.country || '—')}</td>
            <td class="num elo-cell">${Math.round(p.elo_combined || p.elo_individual)}</td>
            <td class="num">${p.matches}</td>
            <td class="num country-cell">${p.last_active || ''}</td>
        </tr>
    `).join('');
    const more = filtered.length > visible.length
        ? `<div class="elo-summary">… ${filtered.length - visible.length} weitere ausgeblendet (Filter verfeinern)</div>`
        : '';

    host.innerHTML = summary + `
        <table class="elo-table">
            <thead><tr>
                <th class="num">#</th>
                <th></th>
                <th>Name</th>
                <th>Land</th>
                <th class="num">ELO</th>
                <th class="num">Matches</th>
                <th class="num">Zuletzt aktiv</th>
            </tr></thead>
            <tbody>${rows}</tbody>
        </table>` + more;
}

function _escapeHtml(s) {
    return String(s || '').replace(/[&<>"']/g, c => ({
        '&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'
    }[c]));
}

// ── ELO Tuning tab ───────────────────────────────────────────────────────────
//
// POSTs custom EloConfig to /api/elo-recompute, shows the resulting top list
// and backtest accuracy. Sandbox only — does NOT touch elo_current.json.

// Tuning state. Sliders are now driven by the server-side schema endpoint
// so each model exposes its own knobs (K/blend for ELO, τ/φ for Glicko-2,
// β/τ/σ for TrueSkill).
let _elotuneState = {};
let _elotuneSchema = null;       // {sliders: [{key,label,min,max,step,default,fmt}, ...]}
let _elotuneModel  = 'elo';
let _elotuneWired = false;

function _fmtVal(v, fmt) {
    if (fmt === 'int')  return Math.round(v).toString();
    if (fmt === 'f1')   return Number(v).toFixed(1);
    if (fmt === 'f2')   return Number(v).toFixed(2);
    if (fmt === 'f3')   return Number(v).toFixed(3);
    return String(v);
}

async function renderEloTuning() {
    if (!_elotuneWired) {
        const sel = document.getElementById('elotuneModelSelect');
        if (sel) {
            sel.value = _elotuneModel;
            sel.addEventListener('change', async () => {
                _elotuneModel = sel.value;
                await _loadElotuneSchema();
            });
        }
        _elotuneWired = true;
    }
    if (!_elotuneSchema || _elotuneSchema.model !== _elotuneModel) {
        await _loadElotuneSchema();
    }
}

async function _loadElotuneSchema() {
    const host = document.getElementById('elotuneSliders');
    if (host) host.innerHTML = '<div class="no-results">Lade Modell-Slider…</div>';
    try {
        const r = await apiFetch(`/api/elo-model-schema?model=${_elotuneModel}`);
        if (!r.ok) throw new Error(r.status + ' ' + r.statusText);
        _elotuneSchema = await r.json();
    } catch (e) {
        if (host) host.innerHTML = `<div class="no-results">
            Schema-Endpoint fehlt (${e.message}).<br>
            <small>serve.py einmal neu starten.</small>
        </div>`;
        return;
    }
    _elotuneState = {};
    for (const s of _elotuneSchema.sliders) {
        _elotuneState[s.key] = s.default;
    }
    _renderTuningSliders();
}

function _renderTuningSliders() {
    const host = document.getElementById('elotuneSliders');
    if (!host || !_elotuneSchema) return;
    host.innerHTML = _elotuneSchema.sliders.map(s => `
        <div class="elotune-slider">
            <label>
                <span>${_escapeHtml(s.label)}</span>
                <button type="button" class="info-icon"
                        onclick="showSliderInfo('${s.key}')"
                        title="Was macht dieser Regler?">i</button>
                <span class="val" data-key="${s.key}" style="margin-left:auto">${_fmtVal(_elotuneState[s.key], s.fmt)}</span>
            </label>
            <input type="range" min="${s.min}" max="${s.max}" step="${s.step}"
                   value="${_elotuneState[s.key]}" data-key="${s.key}">
        </div>
    `).join('');
    host.querySelectorAll('input[type="range"]').forEach(inp => {
        inp.addEventListener('input', () => {
            const k = inp.dataset.key;
            const v = parseFloat(inp.value);
            _elotuneState[k] = v;
            const spec = _elotuneSchema.sliders.find(s => s.key === k);
            host.querySelector(`.val[data-key="${k}"]`).textContent = _fmtVal(v, spec.fmt);
        });
    });
}

function resetEloTuning() {
    if (!_elotuneSchema) return;
    for (const s of _elotuneSchema.sliders) {
        _elotuneState[s.key] = s.default;
    }
    _renderTuningSliders();
}

async function runEloTuning() {
    const btn = document.getElementById('elotuneRun');
    const result = document.getElementById('elotuneResult');
    const useOOS = document.getElementById('elotuneOOS')?.checked;
    if (!btn || !result) return;
    btn.disabled = true;
    btn.textContent = 'Rechne …';

    const cloudMode = !!(window.API_BASE);
    result.innerHTML = `
        <div class="no-results" id="elotuneProgress">
            ⏳ <strong>Rechne ELO über 117k Matches</strong> mit deinen Reglern …<br>
            <div class="progress-indeterminate"></div>
            <small>
                ${cloudMode
                    ? 'Cloud-Modus: kann <strong>mehrere Minuten</strong> dauern. Der Container '
                      + 'muss ~150 MB Roh-HTMLs durch gcsfuse streamen, danach läuft die '
                      + 'Konsolidierung in-memory. Du kannst die Seite zwischendurch verlassen — '
                      + 'das Ergebnis erscheint hier, sobald die Antwort kommt.'
                    : 'Lokal: ~10–15 Sekunden.'}
                <br>Vergangen: <span id="elotuneElapsed">0</span> s
            </small>
        </div>`;
    const t0 = Date.now();
    const timer = setInterval(() => {
        const el = document.getElementById('elotuneElapsed');
        if (el) el.textContent = Math.floor((Date.now() - t0) / 1000);
    }, 1000);

    const body = Object.assign({model: _elotuneModel}, _elotuneState);
    if (useOOS) body.train_end_date = '2024-12-31';

    try {
        const r = await apiFetch('/api/elo-recompute', {
            method: 'POST',
            headers: {'Content-Type': 'application/json'},
            body: JSON.stringify(body),
        });
        if (!r.ok) throw new Error(r.status + ' ' + r.statusText);
        const data = await r.json();
        _drawEloTuneResult(data, Date.now() - t0);
    } catch (e) {
        result.innerHTML = `<div class="no-results">
            Fehler: ${_escapeHtml(e.message)}<br><br>
            <small>Wenn der Endpoint 404 zurückgibt, muss <code>scripts/serve.py</code>
            einmal neu gestartet werden (Strg+C → erneut starten). In der Cloud kann
            ein Timeout auftreten, wenn der Service kalt startet — einmal warten + retry.</small>
        </div>`;
    } finally {
        clearInterval(timer);
        btn.disabled = false;
        btn.textContent = 'Berechne neu';
    }
}

function _drawEloTuneResult(data, clientMs) {
    const result = document.getElementById('elotuneResult');
    if (!result) return;

    const inAcc  = data.in_sample?.accuracy;
    const oosAcc = data.oos?.accuracy;

    let metaCards = `
        <div><span class="stat-label">Spieler</span>
            <span class="stat-val">${data.n_players.toLocaleString('de-DE')}</span></div>
        <div><span class="stat-label">Matches</span>
            <span class="stat-val">${data.n_matches.toLocaleString('de-DE')}</span></div>
        <div><span class="stat-label">In-sample (DVV 25+)</span>
            <span class="stat-val">${inAcc != null ? (inAcc*100).toFixed(1)+'%' : '–'}</span>
            <small style="color:var(--text-dim)">${data.in_sample?.n || 0} matches</small></div>`;
    if (data.oos) {
        metaCards += `
        <div><span class="stat-label">OOS (nach ${data.train_end_date})</span>
            <span class="stat-val">${oosAcc != null ? (oosAcc*100).toFixed(1)+'%' : '–'}</span>
            <small style="color:var(--text-dim)">${data.oos.n} matches</small></div>`;
    }
    metaCards += `
        <div><span class="stat-label">Berechnung (Server)</span>
            <span class="stat-val">${data.duration_s.toFixed(1)}s</span></div>`;

    // Calibration table — prefer OOS when present, else in-sample
    const calibSrc = data.oos?.calibration || data.in_sample?.calibration || [];
    const calibLabel = data.oos ? `OOS Calibration` : `In-sample Calibration`;
    const calibRows = calibSrc.filter(r => r.n > 0).map(r => `
        <tr>
            <td>[${r.bucket_lo.toFixed(1)}, ${(r.bucket_lo+0.1).toFixed(1)})</td>
            <td>${r.n}</td>
            <td>${(r.predicted*100).toFixed(0)}%</td>
            <td>${r.actual != null ? (r.actual*100).toFixed(0)+'%' : '–'}</td>
        </tr>
    `).join('');
    const calibTable = calibRows ? `
        <h4 style="margin:0.5rem 0; color:var(--text-dim); font-size:0.85rem;">${calibLabel}</h4>
        <table class="elotune-calib">
            <thead><tr><th>Bucket</th><th>n</th><th>predicted</th><th>actual</th></tr></thead>
            <tbody>${calibRows}</tbody>
        </table>` : '';

    // Top-30 players (M+W combined) for quick comparison vs the production list
    const top = (data.players || []).slice(0, 30);
    const rows = top.map((p, i) => `
        <tr>
            <td class="rank num">${i + 1}</td>
            <td class="gender-cell">${p.gender === 'm' ? '♂' : p.gender === 'f' ? '♀' : ''}</td>
            <td>${_escapeHtml(p.name)}</td>
            <td class="country-cell">${_escapeHtml(p.country || '—')}</td>
            <td class="num elo-cell">${Math.round(p.elo_combined || p.elo_individual)}</td>
            <td class="num">${p.matches}</td>
            <td class="num country-cell">${p.last_active || ''}</td>
        </tr>
    `).join('');

    result.innerHTML = `
        <div class="elotune-result-meta">${metaCards}</div>
        ${calibTable}
        <table class="elo-table">
            <thead><tr>
                <th class="num">#</th><th></th><th>Name</th><th>Land</th>
                <th class="num">ELO</th><th class="num">M</th><th class="num">Zuletzt</th>
            </tr></thead>
            <tbody>${rows}</tbody>
        </table>
        <div class="elo-summary">Top 30 von ${(data.players || []).length} Spielern. Client-Roundtrip: ${(clientMs/1000).toFixed(1)}s</div>
    `;
}

function switchToTab(tabId) {
    const btn = document.querySelector(`.tab[onclick*="'${tabId}'"]`);
    if (btn) switchTab(tabId, btn);
}

function updatePicksHint() {
    const hint = document.getElementById('usePicksHint');
    if (!hint) return;
    const locked = [...lockedPlayerIds].filter(id => availablePlayers.some(p => p.id === id && p.price > 0));
    const banned = [...bannedPlayerIds].filter(id => availablePlayers.some(p => p.id === id && p.price > 0));
    const parts = [];
    if (locked.length) parts.push(`🔒 ${locked.length} fest`);
    if (banned.length) parts.push(`🚫 ${banned.length} ausgeschlossen`);
    hint.textContent = parts.length ? parts.join(' · ') : '';
}

// ── Bracket prediction view ───────────────────────────────────────────────────

let bracketGender = 'm';
let bracketTree = localStorage.getItem('bracketTree') || 'current';
let _bracketDisplayByNum = {};   // match-num → displayed (tree-derived) match, for the detail modal

// Switch the displayed prediction tree. "personal" enables click-to-edit.
function setBracketTree(tree) {
    bracketTree = tree;
    localStorage.setItem('bracketTree', tree);
    manualMode = (tree === 'personal');
    const modelLbl = document.getElementById('bracketEloModelLabel');
    if (modelLbl) modelLbl.style.display = (tree === 'elo') ? '' : 'none';
    if (tree === 'elo' && !(_eloTreeIndexModel === _eloTreeModel && _eloTreeIndex)) {
        ensureEloTreeData().finally(() => renderBracket());
        return;
    }
    renderBracket();
}
function setEloTreeModel(model) {
    _eloTreeModel = model;
    localStorage.setItem('eloTreeModel', model);
    ensureEloTreeData().finally(() => renderBracket());
}

// ── DVV vs ELO tab: seed-vs-ELO ranking comparison for the current tournament ──
let versusGender = 'm';
let _versusModel = localStorage.getItem('versusModel') || 'ensemble';

function setVersusGender(g) {
    versusGender = g;
    renderVersus();
}
function setVersusModel(model) {
    _versusModel = model;
    localStorage.setItem('versusModel', model);
    renderVersus();
}

// Actual tournament seeding: [{seed, team}] sorted by seed. Derived from the
// bracket-prediction seed refs (S1..Sn → team). Falls back to the teams[] array
// (already seed-ordered) when seed refs aren't present (non-8-team / qualifier draws).
function seedSlotsFor(block) {
    const seen = {}, slots = [];
    for (const m of (block?.bracketPrediction || [])) {
        for (const [ref, team] of [[m.refA, m.teamA], [m.refB, m.teamB]]) {
            if (ref && ref[0] === 'S' && team && team !== 'TBD' && !(ref in seen)) {
                const seed = parseInt(ref.slice(1), 10);
                if (!isNaN(seed)) { seen[ref] = true; slots.push({ seed, team }); }
            }
        }
    }
    if (slots.length) return slots.sort((a, b) => a.seed - b.seed);
    return (block?.teams || []).map((t, i) => ({ seed: i + 1, team: t.name }));
}

async function renderVersus() {
    const view = document.getElementById('versusView');
    if (!view) return;
    // Keep controls in sync with state.
    const modelSel = document.getElementById('versusModelSelect');
    if (modelSel) modelSel.value = _versusModel;
    document.querySelectorAll('#versusGenderToggle .filter-pill').forEach(b => {
        b.classList.toggle('active', b.dataset.bg === versusGender);
    });

    // This comparison only needs the sim file (teams + bracket seeds). If it isn't in
    // memory yet, load whatever exists on disk right away — independent of the bracket /
    // "Team optimieren" flow, so the tab works standalone on first open.
    if (!tournamentSim || !tournamentSim.byGender) {
        await loadSimFile();
    }
    if (!tournamentSim || !tournamentSim.byGender) {
        const banner = document.getElementById('simBanner');
        const loading = banner && banner.dataset.mode === 'loading';
        view.innerHTML = loading
            ? '<div class="no-results"><span class="sim-spinner"></span> Turnierdaten werden berechnet…</div>'
            : '<div class="no-results">Keine Sim-Daten geladen.</div>';
        return;
    }
    const block = tournamentSim.byGender[versusGender];
    if (!block || !block.bracketPrediction || block.bracketPrediction.length === 0) {
        view.innerHTML = `<div class="no-results">Keine Turnierdaten für ${versusGender === 'm' ? 'Männer' : 'Frauen'}.</div>`;
        return;
    }

    const seeds = seedSlotsFor(block);
    if (!seeds.length) {
        view.innerHTML = '<div class="no-results">Keine Setzliste verfügbar.</div>';
        return;
    }

    // ELO index for the selected model (best-effort — null if the JSON can't load).
    const eloIndex = await ensureEloTreeData(_versusModel);
    const maps = buildTeamRatingMaps(block, eloIndex, versusGender);   // {dvv, elo, eloBreak}

    // Rank the seeded teams by ELO desc; teams without ELO sink to the bottom (no delta).
    const withElo = seeds.filter(s => maps.elo[s.team] != null)
                         .sort((a, b) => maps.elo[b.team] - maps.elo[a.team]);
    const withoutElo = seeds.filter(s => maps.elo[s.team] == null);
    const eloRankOf = {};
    withElo.forEach((s, i) => { eloRankOf[s.team] = i + 1; });

    const modelLabel = { elo: 'ELO (klassisch)', glicko2: 'Glicko-2',
                         trueskill: 'TrueSkill', ensemble: 'Ensemble' }[_versusModel] || _versusModel;
    const metaLine = `${block.tournamentName ?? '?'} · ${versusGender === 'm' ? 'Männer' : 'Frauen'} · Modell: ${modelLabel}`;

    const leftRows = seeds.map(s => {
        const pts = maps.dvv[s.team];
        return `<div class="versus-row">
            <span class="versus-rank">${s.seed}</span>
            <span class="versus-team">${_escapeHtml(s.team)}</span>
            <span class="versus-metric">${pts != null ? Math.round(pts) : '—'}</span>
        </div>`;
    }).join('');

    const eloRows = withElo.map(s => {
        const delta = s.seed - eloRankOf[s.team];        // + = ELO ranks higher than seed
        const cls = delta > 0 ? 'delta-up' : (delta < 0 ? 'delta-down' : 'delta-flat');
        const txt = delta > 0 ? `+${delta}` : (delta < 0 ? `${delta}` : '±0');
        const tip = (maps.eloBreak[s.team] || [])
            .map(c => `${c.name}: ${c.rating != null ? Math.round(c.rating) : 'k.A.'}`).join(' · ');
        return `<div class="versus-row" title="${_escapeHtml(tip)}">
            <span class="versus-rank">${eloRankOf[s.team]}</span>
            <span class="versus-team">${_escapeHtml(s.team)}</span>
            <span class="versus-metric">${Math.round(maps.elo[s.team])}</span>
            <span class="versus-delta ${cls}">${txt}</span>
        </div>`;
    }).join('');
    const missingRows = withoutElo.map(s => `<div class="versus-row versus-row-muted">
            <span class="versus-rank">–</span>
            <span class="versus-team">${_escapeHtml(s.team)}</span>
            <span class="versus-metric">—</span>
            <span class="versus-delta delta-flat" title="Kein ELO-Rating verfügbar">—</span>
        </div>`).join('');

    const eloAvailable = eloIndex && withElo.length > 0;
    const rightBody = eloAvailable
        ? eloRows + missingRows
        : '<div class="no-results">ELO-Daten für dieses Modell nicht verfügbar.</div>';

    view.innerHTML = `
        <div class="versus-meta">${_escapeHtml(metaLine)}</div>
        <div class="versus-cols">
            <div class="versus-col">
                <div class="versus-col-head">📊 DVV-Setzliste</div>
                ${leftRows}
            </div>
            <div class="versus-col">
                <div class="versus-col-head">🏅 ELO-Rangliste</div>
                ${rightBody}
            </div>
        </div>`;
}

function renderBracket() {
    const view = document.getElementById('bracketView');
    // Keep the selector controls in sync with state.
    const treeSel = document.getElementById('bracketTreeSelect');
    if (treeSel) treeSel.value = bracketTree;
    const modelSel = document.getElementById('bracketEloModelSelect');
    if (modelSel) modelSel.value = _eloTreeModel;
    const modelLbl = document.getElementById('bracketEloModelLabel');
    if (modelLbl) modelLbl.style.display = (bracketTree === 'elo') ? '' : 'none';

    if (!tournamentSim || !tournamentSim.byGender) {
        const banner = document.getElementById('simBanner');
        const loading = banner && banner.dataset.mode === 'loading';
        view.innerHTML = loading
            ? '<div class="no-results"><span class="sim-spinner"></span> Bracket-Vorhersage wird berechnet…</div>'
            : '<div class="no-results">Keine Sim-Daten geladen.</div>';
        return;
    }
    const block = tournamentSim.byGender[bracketGender];
    if (!block || !block.bracketPrediction || block.bracketPrediction.length === 0) {
        view.innerHTML = `<div class="no-results">Keine Bracket-Vorhersage für ${bracketGender === 'm' ? 'Männer' : 'Frauen'}.</div>`;
        return;
    }
    if (bracketTree === 'elo' && !(_eloTreeIndexModel === _eloTreeModel && _eloTreeIndex)) {
        view.innerHTML = '<div class="no-results"><span class="sim-spinner"></span> ELO-Daten werden geladen…</div>';
        ensureEloTreeData().finally(() => renderBracket());
        return;
    }

    // Re-derive the bracket for the selected tree (played matches stay locked).
    const treeMap = deriveTreeBracket(bracketGender, bracketTree, _eloTreeIndex) || {};
    const isPersonal = (bracketTree === 'personal');
    manualMode = isPersonal;
    const matches  = block.bracketPrediction.map(bm => treeMap[bm.match]).filter(Boolean);
    const hasOverrides = isPersonal && Object.keys(getManualOverrides()).length > 0;
    const byNum    = Object.fromEntries(matches.map(m => [m.match, m]));
    _bracketDisplayByNum = byNum;
    const treeName = TREE_LABELS[bracketTree] ?? bracketTree;
    const meta     = `${block.tournamentName ?? '?'} · Status: ${block.bracketStatus ?? '?'} · Baum: ${treeName}`;

    // GBT 8-team double-elim layout — uses an 8-row grid per column.
    // start/end are inclusive grid lines (1..9).
    const columns = [
        {
            title: 'Achtelfinale Winner',
            entries: [
                { num: 1, start: 1, end: 3 },
                { num: 2, start: 3, end: 5 },
                { num: 3, start: 5, end: 7 },
                { num: 4, start: 7, end: 9 },
            ],
        },
        {
            title: 'Viertelfinale Winner',
            entries: [
                { num: 7, start: 2, end: 4 },
                { num: 8, start: 6, end: 8 },
            ],
        },
        {
            title: 'Halbfinale & Finale',
            entries: [
                { num: 11, start: 1, end: 3 },
                { num: 13, start: 4, end: 6, finale: true },
                { num: 12, start: 7, end: 9 },
            ],
        },
        {
            title: 'Viertelfinale Loser',
            entries: [
                { num: 9,  start: 2, end: 4 },
                { num: 10, start: 6, end: 8 },
            ],
        },
        {
            title: 'Achtelfinale Loser',
            entries: [
                { num: 5, start: 2, end: 4 },
                { num: 6, start: 6, end: 8 },
            ],
        },
    ];

    const colsHtml = columns.map(col => {
        const cards = col.entries.map(e => {
            const m = byNum[e.num];
            if (!m) return '';
            // In personal mode pass the derived match so the ✏ override badge +
            // click-to-edit affordances light up; other trees render m directly.
            const dm = isPersonal ? m : null;
            return matchCard(m, e.start, e.end, e.finale === true, dm);
        }).join('');
        return `
        <div class="br-col">
            <div class="br-col-title">${col.title}</div>
            <div class="br-col-body">${cards}</div>
        </div>`;
    }).join('');

    const manualHint = manualMode
        ? `<p class="tab-hint br-manual-hint">
               ✏ Persönlicher Baum — klicke auf einen Teamnamen um den Sieger dieses Spiels zu überschreiben.
               Nochmals klicken hebt die Überschreibung auf.
               Die Anpassungen fließen als <strong>Turnier-Prognose (Persönlich)</strong> und
               <strong>Finale-Fokus (Persönlich)</strong> in den Vergleichstab ein.
           </p>`
        : `<p class="tab-hint">
               <strong>Bold</strong> = vorhergesagter Sieger ·
               <span class="br-h2h-mark">H2H</span> = direkte Team-Bilanz (≥3 Spiele) ·
               <span class="br-h2h-mark br-ind-mark">👤</span> = Einzel-H2H-Bilanz ·
               <span class="br-h2h-mark br-seed-mark">📌</span> = Setzliste geschätzt ·
               sonst DVV-Punkte · grau = Verlierer-Pfad
           </p>`;

    view.innerHTML = `
        <div class="br-meta">${escapeHtml(meta)}</div>
        <div class="br-grid">${colsHtml}</div>
        ${manualHint}`;

    // Sync "Zurücksetzen"-Button visibility
    const clearBtn = document.getElementById('clearManualBtn');
    if (clearBtn) clearBtn.style.display = hasOverrides ? '' : 'none';
}

// dm = derived match object (only present in manual mode); null → use auto prediction
function matchCard(m, rowStart, rowEnd, isFinale, dm) {
    // In manual mode show derived teams/winner; otherwise use auto prediction
    const display = dm || m;
    const wA = display.winner === display.teamA;
    const wB = display.winner === display.teamB;
    const pAClass = wA ? 'br-winner' : 'br-loser';
    const pBClass = wB ? 'br-winner' : 'br-loser';
    const probAStr = (m.probA * 100).toFixed(0);
    const probBStr = (m.probB * 100).toFixed(0);
    const showQ = m.reason === 'no_data';

    const finaleHeader = isFinale ? '<div class="br-finale-trophy">🏆 FINALE</div>' : '';
    const badges = [];
    if (!manualMode && m.reason === 'h2h')     badges.push('<span class="br-h2h-mark">H2H</span>');
    if (!manualMode && m.reason === 'h2h_ind') badges.push('<span class="br-h2h-mark br-ind-mark" title="Einzel-H2H-Bilanz">👤</span>');
    if (!manualMode && m.reason === 'close')   badges.push('<span class="br-h2h-mark br-close-mark" title="Knapp">~</span>');
    if (!manualMode && m.reason === 'seeding') badges.push('<span class="br-h2h-mark br-seed-mark" title="Setzliste geschätzt">📌</span>');
    if (!manualMode && m.reason === 'elo')     badges.push('<span class="br-h2h-mark br-elo-mark" title="ELO-Rating">🏅</span>');
    if (!manualMode && m.reason === 'played')  badges.push('<span class="br-h2h-mark br-played-mark" title="Gespielt">✅</span>');
    if (dm?.overridden)  badges.push('<span class="br-h2h-mark br-manual-mark" title="Manuell überschrieben">✏</span>');
    const badgesHtml = badges.join(' ');

    const clickAttr = manualMode
        ? ''  // clicks go to team-name buttons
        : `onclick="openMatchDetail(${m.match})" title="Klick für Details"`;

    const teamRow = (teamName, probStr, cls) => {
        const nameHtml = manualMode
            // Use data attributes — avoids inline quote-escaping issues with team names
            ? `<span class="br-team-name br-team-clickable"
                   data-match="${m.match}"
                   data-team="${escapeHtml(teamName)}"
                   onclick="handleTeamClick(this, event)"
                   title="Als Sieger setzen">${escapeHtml(teamName)}</span>`
            : `<span class="br-team-name">${escapeHtml(teamName)}</span>`;
        const probHtml = manualMode
            ? ''
            : `<span class="br-prob">${showQ ? '?' : probStr + '%'}</span>`;
        return `<div class="br-team ${cls} ${manualMode ? 'br-team-manual' : ''}">${nameHtml}${probHtml}</div>`;
    };

    return `
    <div class="br-match ${isFinale ? 'br-match-finale' : ''} ${manualMode ? 'br-match-edit' : ''}"
         style="grid-row:${rowStart}/${rowEnd}"
         ${clickAttr}>
        ${finaleHeader}
        <div class="br-match-head">
            <span class="br-match-num">Spiel ${m.match}</span>
            ${badgesHtml}
        </div>
        ${teamRow(display.teamA, probAStr, pAClass, true)}
        ${teamRow(display.teamB, probBStr, pBClass, false)}
    </div>`;
}

function setBracketGender(g) {
    bracketGender = g;
    document.querySelectorAll('#bracketGenderToggle .filter-pill').forEach(b => {
        b.classList.toggle('active', b.dataset.bg === g);
    });
    renderBracket();
}

// ── Match-detail modal: explains how the prediction was computed ─────────────

function openMatchDetail(matchNum) {
    const block = tournamentSim?.byGender?.[bracketGender];
    // Prefer the currently-displayed tree's match (teams/probs match what's on
    // screen); fall back to the base prediction.
    const m = _bracketDisplayByNum[matchNum]
        || block?.bracketPrediction?.find(x => x.match === matchNum);
    if (!m) return;
    closeMatchDetail();

    const modal = document.createElement('div');
    modal.id = 'matchDetailModal';
    modal.className = 'amb-modal';
    modal.onclick = (e) => { if (e.target === modal) closeMatchDetail(); };

    const reasonLabel = {
        h2h:        '🤝 Direkte Team-Bilanz (Head-to-Head, ≥&nbsp;3 Spiele)',
        h2h_ind:    '👤 Einzel-H2H-Bilanz — Team-H2H nicht eindeutig, aber individuelle Spielerpaarungen klar (&gt;&nbsp;10&nbsp;% Differenz)',
        dvv:        '📊 DVV Ranking-Differenz (&gt;&nbsp;10&nbsp;%)',
        seeding:    '📌 Setzliste-Schätzung — mind. ein Team ohne DVV-Daten, Punkte aus Turnier-Setzliste interpoliert',
        ranking:    '📊 DVV Ranking-Differenz',
        close:      '⚖ Knappes Spiel — DVV-Differenz ≤&nbsp;10&nbsp;%; H2H als Tiebreaker',
        fifty_fifty:'🎲 50/50 — kein ausreichendes H2H und knappe DVV-Werte',
        no_data:    '❓ Keine Daten verfügbar — 50/50',
        elo:        '🏅 ELO-Rating-Differenz (gewähltes Modell)',
        played:     '✅ Bereits gespielt — echtes Ergebnis',
        manual:     '✏ Manuell gesetzt',
    }[m.reason] || m.reason;

    const probAStr = (m.probA * 100).toFixed(1);
    const probBStr = (m.probB * 100).toFixed(1);
    // Show ? only when truly no data
    const showQ = m.reason === 'no_data' || m.reason === 'fifty_fifty';

    modal.innerHTML = `
        <div class="amb-dialog">
            <div class="amb-header">
                <div>
                    <h2 style="margin:0">Spiel ${m.match} — Vorhersage</h2>
                    <p style="margin:0.3rem 0 0;font-size:0.85rem;color:var(--text-dim)">${reasonLabel}</p>
                </div>
                <button class="amb-close" onclick="closeMatchDetail()">×</button>
            </div>
            <div class="amb-body">
                <div class="md-result">
                    <div class="md-team ${m.winner === m.teamA ? 'md-winner' : 'md-loser'}">
                        <span class="md-team-name">${escapeHtml(m.teamA)}</span>
                        <span class="md-prob">${showQ ? '?' : probAStr + '%'}</span>
                    </div>
                    <div class="md-vs">vs</div>
                    <div class="md-team ${m.winner === m.teamB ? 'md-winner' : 'md-loser'}">
                        <span class="md-team-name">${escapeHtml(m.teamB)}</span>
                        <span class="md-prob">${showQ ? '?' : probBStr + '%'}</span>
                    </div>
                </div>

                ${renderRatingSection(m)}
                ${renderH2HSection(m)}
                ${renderIndividualH2HSection(m)}
                ${renderTraceSection(m.teamA, m.ptsA, m.traceA)}
                ${renderTraceSection(m.teamB, m.ptsB, m.traceB)}
            </div>
            <div class="amb-footer">
                <button class="btn-inline" onclick="closeMatchDetail()">Schließen</button>
            </div>
        </div>`;
    document.body.appendChild(modal);
}

function closeMatchDetail() {
    document.getElementById('matchDetailModal')?.remove();
}

// ── Manual bracket override helpers ───────────────────────────────────────────

function _manualKeyFor(gender) {
    const block = tournamentSim?.byGender?.[gender];
    return `manualOverrides_${gender}_${block?.tournamentId ?? 'x'}`;
}

function _manualKey() {
    return _manualKeyFor(bracketGender);
}

function getManualOverridesFor(gender) {
    try { return JSON.parse(localStorage.getItem(_manualKeyFor(gender)) || '{}'); }
    catch { return {}; }
}

function getManualOverrides() {
    return getManualOverridesFor(bracketGender);
}

function saveManualOverrides(ov) {
    localStorage.setItem(_manualKey(), JSON.stringify(ov));
    _manualOverridesCache = null;
}

function clearManualOverrides() {
    localStorage.removeItem(_manualKey());
    _manualOverridesCache = null;
    document.getElementById('clearManualBtn').style.display = 'none';
    renderBracket();
}

function setManualWinner(matchNum, teamName) {
    const ov = getManualOverrides();
    // Clicking the current winner again → removes the override
    if (ov[matchNum] === teamName) {
        delete ov[matchNum];
    } else {
        ov[matchNum] = teamName;
    }
    saveManualOverrides(ov);
    const clearBtn = document.getElementById('clearManualBtn');
    if (clearBtn) clearBtn.style.display = Object.keys(ov).length ? '' : 'none';
    renderBracket();
}

function toggleManualMode(on) {
    manualMode = on;
    renderBracket();
}

// Called via data-attributes — avoids inline string-escaping issues
function handleTeamClick(el, evt) {
    evt.stopPropagation();
    const matchNum = parseInt(el.dataset.match, 10);
    const teamName = el.dataset.team;
    setManualWinner(matchNum, teamName);
}

// Re-derives the full bracket deterministically, respecting manual overrides.
// Returns {matchNum: {teamA, teamB, winner, loser, overridden}}
function deriveManualBracket(basePrediction, overrides) {
    const sorted = [...basePrediction].sort((a, b) => a.match - b.match);

    // Capture seed→team from the base prediction (S-refs never change)
    const seedSlots = {};
    for (const m of sorted) {
        if (m.refA && m.refA[0] === 'S' && m.teamA && m.teamA !== 'TBD') seedSlots[m.refA] = m.teamA;
        if (m.refB && m.refB[0] === 'S' && m.teamB && m.teamB !== 'TBD') seedSlots[m.refB] = m.teamB;
    }

    const results = {};

    function resolveRef(ref) {
        if (!ref) return null;
        const prefix = ref[0], num = parseInt(ref.slice(1));
        if (prefix === 'S') return seedSlots[ref] ?? null;
        if (prefix === 'W') return results[num]?.winner ?? null;
        if (prefix === 'L') return results[num]?.loser  ?? null;
        return null;
    }

    for (const m of sorted) {
        const teamA = resolveRef(m.refA) ?? m.teamA ?? 'TBD';
        const teamB = resolveRef(m.refB) ?? m.teamB ?? 'TBD';
        const overridden = m.match in overrides;
        let winner, loser;
        if (overridden) {
            winner = overrides[m.match];
            loser  = (winner === teamA) ? teamB : teamA;
        } else {
            winner = (m.probA >= m.probB) ? teamA : teamB;
            loser  = (winner === teamA) ? teamB : teamA;
        }
        results[m.match] = { match: m.match, teamA, teamB, winner, loser, overridden };
    }
    return results;
}

// ── Tournament-tree prediction (current / dvv / elo / personal) ────────────────
//
// A "tree" is one set of per-match win probabilities. deriveTreeBracket()
// re-derives the whole bracket deterministically for the chosen method,
// resolving W/L refs from the computed winners so later-round matchups reflect
// that method. Already-played matches (base reason==='played') stay LOCKED to
// the real result for every tree. From the derived bracket we read per-player
// expected matches (→ Turnier-Prognose) and round levels (→ Finale-Fokus).

const TREE_LABELS = { current: 'Aktuell', dvv: 'DVV-Punkte', elo: 'ELO', personal: 'Persönlich' };
const TREE_ICONS  = { current: '🎯', dvv: '📊', elo: '🏅', personal: '✏' };
const TREE_ORDER  = ['current', 'dvv', 'elo', 'personal'];

let _eloTreeModel = localStorage.getItem('eloTreeModel') || 'ensemble';
let _eloTreeIndex = null;          // { eloId: elo_combined } for _eloTreeIndexModel
let _eloTreeIndexModel = null;     // which model _eloTreeIndex was built for

// Mirror of scripts/elo/priors.py::_normalise (NFKD, strip marks, lower, collapse ws).
function _normName(s) {
    return (s || '').normalize('NFKD').replace(/[̀-ͯ]/g, '')
        .toLowerCase().split(/\s+/).filter(Boolean).join(' ');
}
function eloIdFromName(first, last) {
    return `${_normName(last)}_${_normName(first)}`.replace(/^_+|_+$/g, '');
}

// Fetch the selected ELO model JSON (reusing the ranking-tab cache) and build a
// name→rating index. Best-effort: on failure the ELO tree is simply unavailable.
async function ensureEloTreeData(model = _eloTreeModel) {
    // Fast path only for the bracket-tab's own selected model (whose index is cached
    // in the module globals). Other callers (e.g. the DVV-vs-ELO tab) may request a
    // different model — we build a fresh index for them WITHOUT touching those globals.
    const isDefault = (model === _eloTreeModel);
    if (isDefault && _eloTreeIndexModel === model && _eloTreeIndex) return _eloTreeIndex;
    let data = _eloDataByModel[model];
    if (!data) {
        try {
            const r = await apiFetch(`data/${model}_current.json?t=` + Date.now());
            if (!r.ok) throw new Error(r.status);
            data = await r.json();
            _eloDataByModel[model] = data;
        } catch {
            if (isDefault) { _eloTreeIndex = null; _eloTreeIndexModel = model; }
            return null;
        }
    }
    const idx = {};
    const byLast = {};   // surname → [{name, rating, gender, matches, last_active}] for the fallback
    for (const pl of (data.players || [])) {
        if (pl.id == null || pl.elo_combined == null) continue;
        idx[pl.id] = pl.elo_combined;
        const us = String(pl.id).indexOf('_');   // id is "{last}_{first}", last has no underscore
        const last = us >= 0 ? String(pl.id).slice(0, us) : String(pl.id);
        (byLast[last] = byLast[last] || []).push({
            name: pl.name || null, rating: pl.elo_combined, gender: pl.gender || null,
            matches: pl.matches || 0, last_active: pl.last_active || '',
        });
    }
    // Attach the surname index non-enumerably so key-iteration / lookups on idx stay unaffected.
    Object.defineProperty(idx, '__byLast', { value: byLast, enumerable: false });
    if (isDefault) { _eloTreeIndex = idx; _eloTreeIndexModel = model; }
    return idx;
}

// Per-team rating lookups for a gender block.
// dvv: team-name → DVV points; elo: team-name → mean elo_combined (absent = unmapped).
function buildTeamRatingMaps(block, eloIndex, gender = null) {
    const byId = {};
    for (const p of allPlayers) byId[p.id] = p;
    // Surname fallback for international players. They aren't in the Firestore roster
    // (allPlayers), so their bracket slots can't be resolved via playerIds — but the ELO
    // dataset (FIVB/bvb) usually still holds them. `__byLast` (attached by ensureEloTreeData)
    // maps surname → candidate records; we resolve a slot only when it's unambiguous.
    const eloByLast = (eloIndex && eloIndex.__byLast) || {};
    const bg = gender || block.gender || null;   // 'm' | 'f' | null
    // Gender-narrowed candidate list for a surname (already normalised key).
    const candsFor = (nl) => {
        let cands = eloByLast[nl] || [];
        if (bg && cands.some(c => c.gender)) {
            const g = cands.filter(c => !c.gender || c.gender === bg);
            if (g.length) cands = g;
        }
        return cands;
    };
    // Surname variants to try, full name first, then hyphen/space components (≥3 chars).
    // The DVV Setzliste sometimes carries double-barrelled surnames ("Chouikh-Barbez")
    // where the ELO dataset (FIVB) only lists one component ("Chouikh").
    const surnameVariants = (nl) => {
        const keys = [nl];
        for (const part of nl.split(/[-\s]+/)) if (part.length >= 3 && part !== nl) keys.push(part);
        return keys;
    };
    // Best candidate list of exactly `need` players for a surname, or null. Tries each
    // variant; accepts a variant when its full (or active-only) candidate set matches `need`.
    const pickForSurname = (nl, need) => {
        for (const key of surnameVariants(nl)) {
            const cands = candsFor(key);
            if (!cands.length) continue;
            if (cands.length === need) return cands;
            const active = cands.filter(c => c.last_active >= '2025' && c.matches >= 20);
            if (active.length === need) return active;
        }
        return null;
    };
    const dvv = {}, elo = {}, eloBreak = {};
    for (const t of (block.teams || [])) {
        if (t.dvvPoints != null) dvv[t.name] = t.dvvPoints;
        if (!eloIndex) continue;
        // Build the team's two slots: prefer Firestore-roster identities (have first+last),
        // otherwise fall back to the surnames parsed from the team name ("LastA - LastB").
        const slots = [];
        const covered = {};   // normLast → how many roster slots already took it
        for (const pid of (t.playerIds || [])) {
            const p = byId[pid];
            if (!p) continue;
            const nl = _normName(p.lastName);
            slots.push({ name: p.name, last: nl, first: p.firstName, rating: null });
            covered[nl] = (covered[nl] || 0) + 1;
        }
        for (const raw of String(t.name || '').split(' - ')) {
            const last = raw.trim(); if (!last) continue;
            const nl = _normName(last);
            if (covered[nl]) { covered[nl]--; continue; }   // roster already accounts for it
            slots.push({ name: last, last: nl, first: null, rating: null });
        }
        // Pass 1: exact "{last}_{first}" id for roster slots.
        for (const s of slots) {
            if (s.first) { const r = eloIndex[eloIdFromName(s.first, s.last)]; if (r != null) s.rating = r; }
        }
        // Pass 2: surname fallback for international / rookie slots (no Firestore identity or
        // an exact-id miss). Count-aware: assign only when the number of plausible candidates
        // for a surname exactly matches the number of still-unresolved slots needing it — this
        // resolves unique surnames AND same-surname pairs (e.g. "Klinger - Klinger" → both
        // active female Klingers), while staying blank when it's genuinely ambiguous.
        const needBy = {};
        for (const s of slots) if (s.rating == null) (needBy[s.last] = needBy[s.last] || []).push(s);
        for (const nl in needBy) {
            const pick = pickForSurname(nl, needBy[nl].length);
            if (pick) needBy[nl].forEach((s, i) => {
                s.rating = pick[i].rating;
                if (!s.first && pick[i].name) s.name = pick[i].name;   // enrich bare surname → full name
            });
        }
        const comps = slots.map(s => ({ name: s.name, rating: s.rating }));
        const ratings = slots.filter(s => s.rating != null).map(s => s.rating);
        if (comps.length) eloBreak[t.name] = comps;
        if (ratings.length) elo[t.name] = ratings.reduce((a, b) => a + b, 0) / ratings.length;
    }
    // Fill any DVV gaps from the match points fields.
    for (const m of (block.bracketPrediction || [])) {
        if (m.teamA && dvv[m.teamA] == null && m.ptsA != null) dvv[m.teamA] = m.ptsA;
        if (m.teamB && dvv[m.teamB] == null && m.ptsB != null) dvv[m.teamB] = m.ptsB;
    }
    return { dvv, elo, eloBreak };
}

// Re-derive the full bracket for one gender + tree. Returns {matchNum: {...}}
// with display-ready fields (teamA/teamB/winner/loser/probA/reason/overridden).
function deriveTreeBracket(gender, tree, eloIndex) {
    const block = tournamentSim?.byGender?.[gender];
    if (!block?.bracketPrediction || !block?.teams) return null;
    const base = [...block.bracketPrediction].sort((a, b) => a.match - b.match);
    const overrides = (tree === 'personal') ? getManualOverridesFor(gender) : {};
    const useRatings = (tree === 'dvv' || tree === 'elo');
    const maps = useRatings ? buildTeamRatingMaps(block, eloIndex, gender) : null;

    const seedSlots = {};
    for (const m of base) {
        if (m.refA && m.refA[0] === 'S' && m.teamA && m.teamA !== 'TBD') seedSlots[m.refA] = m.teamA;
        if (m.refB && m.refB[0] === 'S' && m.teamB && m.teamB !== 'TBD') seedSlots[m.refB] = m.teamB;
    }
    const results = {};
    const resolveRef = (ref) => {
        if (!ref) return null;
        const pfx = ref[0], n = parseInt(ref.slice(1));
        if (pfx === 'S') return seedSlots[ref] ?? null;
        if (pfx === 'W') return results[n]?.winner ?? null;
        if (pfx === 'L') return results[n]?.loser ?? null;
        return null;
    };
    // Rating-based probability that teamA beats teamB. Returns {p, fell} where
    // fell=true means ELO fell back to the DVV ratio (a team was unmapped).
    const ratingProb = (a, b) => {
        if (tree === 'dvv') {
            const pa = maps.dvv[a] ?? 0, pb = maps.dvv[b] ?? 0;
            return { p: (pa + pb) > 0 ? pa / (pa + pb) : 0.5, fell: false };
        }
        const ra = maps.elo[a], rb = maps.elo[b];
        if (ra == null || rb == null) {
            const pa = maps.dvv[a] ?? 0, pb = maps.dvv[b] ?? 0;
            return { p: (pa + pb) > 0 ? pa / (pa + pb) : 0.5, fell: true };
        }
        return { p: 1 / (1 + Math.pow(10, (rb - ra) / 400)), fell: false };
    };

    for (const bm of base) {
        const teamA = resolveRef(bm.refA) ?? bm.teamA ?? 'TBD';
        const teamB = resolveRef(bm.refB) ?? bm.teamB ?? 'TBD';
        const overridden = (bm.match in overrides);
        let probA, reason, winner, loser;
        if (bm.reason === 'played') {
            // Locked to the real result (teams already fixed).
            winner = bm.winner; loser = bm.loser;
            probA = (winner === teamA) ? 1 : 0; reason = 'played';
        } else if (overridden) {
            winner = overrides[bm.match]; loser = (winner === teamA) ? teamB : teamA;
            probA = (winner === teamA) ? 1 : 0; reason = 'manual';
        } else if (teamA === 'TBD' || teamB === 'TBD') {
            probA = 0.5; reason = 'no_data';
            winner = (teamA !== 'TBD') ? teamA : teamB;
            loser = (winner === teamA) ? teamB : teamA;
        } else if (useRatings) {
            const { p, fell } = ratingProb(teamA, teamB);
            probA = p; reason = (tree === 'elo' && !fell) ? 'elo' : 'dvv';
            winner = probA >= 0.5 ? teamA : teamB;
            loser = (winner === teamA) ? teamB : teamA;
        } else {
            // current / personal (non-overridden) — reuse the base prediction.
            probA = bm.probA; reason = bm.reason;
            winner = (bm.probA >= bm.probB) ? teamA : teamB;
            loser = (winner === teamA) ? teamB : teamA;
        }
        const carryBase = !useRatings;   // keep H2H/trace only for current/personal
        results[bm.match] = {
            match: bm.match, teamA, teamB, refA: bm.refA, refB: bm.refB,
            probA: Math.round(probA * 1000) / 1000,
            probB: Math.round((1 - probA) * 1000) / 1000,
            winner, loser, overridden, reason,
            h2hUsed: reason === 'h2h' || reason === 'h2h_ind',
            h2h: carryBase ? bm.h2h : null,
            indBreakdown: carryBase ? bm.indBreakdown : null,
            ptsA: maps ? (maps.dvv[teamA] ?? bm.ptsA) : bm.ptsA,
            ptsB: maps ? (maps.dvv[teamB] ?? bm.ptsB) : bm.ptsB,
            traceA: carryBase ? bm.traceA : null,
            traceB: carryBase ? bm.traceB : null,
            // ELO team rating + per-player breakdown (for the detail modal).
            eloA: maps ? (maps.elo[teamA] ?? null) : null,
            eloB: maps ? (maps.elo[teamB] ?? null) : null,
            eloBreakA: maps ? (maps.eloBreak[teamA] || null) : null,
            eloBreakB: maps ? (maps.eloBreak[teamB] || null) : null,
        };
    }
    return results;
}

function _expectedMatchesFromDerived(derived, teams) {
    const teamMatches = {};
    for (const mr of Object.values(derived)) {
        if (mr.teamA && mr.teamA !== 'TBD') teamMatches[mr.teamA] = (teamMatches[mr.teamA] || 0) + 1;
        if (mr.teamB && mr.teamB !== 'TBD') teamMatches[mr.teamB] = (teamMatches[mr.teamB] || 0) + 1;
    }
    const out = {};
    for (const t of teams) {
        const em = teamMatches[t.name] ?? 0;
        for (const pid of (t.playerIds || [])) out[pid] = em;
    }
    return out;
}

function _roundLevelsFromDerived(derived, base, teams) {
    const nums = Object.keys(derived).map(Number).sort((a, b) => a - b);
    if (!nums.length) return {};
    const finalNum = nums[nums.length - 1];
    const finalDef = base.find(m => m.match === finalNum);
    const semiNums = new Set();
    for (const ref of [finalDef?.refA, finalDef?.refB]) {
        if (ref && ref[0] === 'W') semiNums.add(parseInt(ref.slice(1)));
    }
    const teamRound = {};
    const fin = derived[finalNum];
    if (fin) {
        if (fin.teamA && fin.teamA !== 'TBD') teamRound[fin.teamA] = 2;
        if (fin.teamB && fin.teamB !== 'TBD') teamRound[fin.teamB] = 2;
    }
    for (const sn of semiNums) {
        const semi = derived[sn];
        if (!semi) continue;
        if (semi.teamA && semi.teamA !== 'TBD' && !teamRound[semi.teamA]) teamRound[semi.teamA] = 1;
        if (semi.teamB && semi.teamB !== 'TBD' && !teamRound[semi.teamB]) teamRound[semi.teamB] = 1;
    }
    const out = {};
    for (const t of teams) {
        const rv = teamRound[t.name] ?? 0;
        for (const pid of (t.playerIds || [])) out[pid] = rv;
    }
    return out;
}

// Per-player expected matches / round level for a tree, merged across genders.
function expectedMatchesFor(tree, eloIndex) {
    const merged = {};
    for (const g of ['m', 'f']) {
        const block = tournamentSim?.byGender?.[g];
        if (!block?.bracketPrediction || !block?.teams) continue;
        const d = deriveTreeBracket(g, tree, eloIndex);
        if (!d) continue;
        const em = _expectedMatchesFromDerived(d, block.teams);
        for (const [pid, v] of Object.entries(em)) merged[pid] = Math.max(merged[pid] ?? 0, v);
    }
    return merged;
}
function roundLevelsFor(tree, eloIndex) {
    const merged = {};
    for (const g of ['m', 'f']) {
        const block = tournamentSim?.byGender?.[g];
        if (!block?.bracketPrediction || !block?.teams) continue;
        const d = deriveTreeBracket(g, tree, eloIndex);
        if (!d) continue;
        const rl = _roundLevelsFromDerived(d, block.bracketPrediction, block.teams);
        for (const [pid, v] of Object.entries(rl)) merged[pid] = Math.max(merged[pid] ?? 0, v);
    }
    return merged;
}

// Which trees are currently usable (drives which panels appear).
function treeAvailable(tree, eloIndex) {
    if (!tournamentSim?.byGender) return false;
    const anyBracket = ['m', 'f'].some(g => {
        const b = tournamentSim.byGender[g];
        return b?.bracketPrediction?.length && b?.teams?.length;
    });
    if (!anyBracket) return false;
    if (tree === 'personal') return ['m', 'f'].some(g => Object.keys(getManualOverridesFor(g)).length);
    if (tree === 'elo') {
        if (!eloIndex) return false;
        return ['m', 'f'].some(g => {
            const b = tournamentSim.byGender[g];
            if (!b?.teams) return false;
            return Object.keys(buildTeamRatingMaps(b, eloIndex, g).elo).length > 0;
        });
    }
    return true; // current, dvv
}
function availableTrees(eloIndex) {
    return TREE_ORDER.filter(t => treeAvailable(t, eloIndex));
}
function algLabel(alg) {
    if (ALG_LABELS[alg]) return ALG_LABELS[alg];
    if (alg.startsWith('prog-')) {
        const t = alg.slice(5);
        return { name: `Turnier-Prognose (${TREE_LABELS[t] ?? t})`, icon: TREE_ICONS[t] ?? '🎯',
                 desc: `${TREE_LABELS[t] ?? t}-Baum × Match-Schnitt` };
    }
    if (alg.startsWith('final-')) {
        const t = alg.slice(6);
        return { name: `Finale-Fokus (${TREE_LABELS[t] ?? t})`, icon: '🏆',
                 desc: `HF/Finale laut ${TREE_LABELS[t] ?? t}-Baum` };
    }
    return { name: alg, icon: '', desc: '' };
}
function objectiveMeta(alg) {
    if (OBJECTIVE_META[alg]) return OBJECTIVE_META[alg];
    if (alg.startsWith('prog-'))  return { label: 'Erw. Punkte (Baum)', short: 'Erw.', digits: 0 };
    if (alg.startsWith('final-')) return { label: 'Final-Round Score',  short: 'F-Score', digits: 0 };
    return { label: 'Score', short: 'val', digits: 1 };
}

function renderH2HSection(m) {
    if (!m.h2h) return '';
    const h = m.h2h;
    const used = m.reason === 'h2h'
        ? '<span class="md-tag md-tag-good">✓ verwendet (≥3 Spiele)</span>'
        : (h.total >= 3
            ? '<span class="md-tag md-tag-warn">⚠ ignoriert (Einzel-H2H war eindeutiger)</span>'
            : '<span class="md-tag md-tag-warn">⚠ ignoriert (zu wenige Spiele)</span>');
    return `
    <div class="md-section">
        <div class="md-section-title">Head-to-Head Bilanz ${used}</div>
        <div class="md-h2h-row">
            <span>${escapeHtml(m.teamA)}</span>
            <strong class="md-score">${h.winsA} : ${h.winsB}</strong>
            <span>${escapeHtml(m.teamB)}</span>
        </div>
        <div class="md-h2h-meta">${h.total} direkte ${h.total === 1 ? 'Begegnung' : 'Begegnungen'} insgesamt</div>
    </div>`;
}

function renderIndividualH2HSection(m) {
    if (!m.indBreakdown?.length) return '';
    const rows = m.indBreakdown.map(r => {
        const total = r.wA + r.wB;
        const pct = (r.wA / total * 100).toFixed(0);
        const winnerA = r.wA > r.wB;
        return `
        <div class="md-h2h-row md-ind-row">
            <span class="${winnerA ? 'md-ind-winner' : ''}">${escapeHtml(r.playerA)}</span>
            <strong class="md-score">${r.wA} : ${r.wB}</strong>
            <span class="${!winnerA ? 'md-ind-winner' : ''}">${escapeHtml(r.playerB)}</span>
            <span class="md-ind-pct">${winnerA ? pct + '&nbsp;%' : (100 - +pct) + '&nbsp;%'} für ${escapeHtml(winnerA ? r.playerA : r.playerB)}</span>
        </div>`;
    }).join('');

    const totalGames = m.indBreakdown.reduce((s, r) => s + r.wA + r.wB, 0);
    const totalWins  = m.indBreakdown.reduce((s, r) => s + r.wA, 0);
    const avg = totalWins / totalGames;
    const usedTag = m.reason === 'h2h_ind'
        ? '<span class="md-tag md-tag-good">✓ verwendet</span>'
        : '<span class="md-tag md-tag-warn">⚠ vorhanden, aber Team-H2H war eindeutiger</span>';

    return `
    <div class="md-section">
        <div class="md-section-title">Einzel-H2H Bilanz ${usedTag}</div>
        ${rows}
        <div class="md-h2h-meta">
            Gewichteter Durchschnitt:
            <strong>${totalWins}</strong> Siege aus <strong>${totalGames}</strong> Spielen
            = <strong>${(avg * 100).toFixed(1)}&nbsp;%</strong> für ${escapeHtml(m.teamA)}
        </div>
    </div>`;
}

function renderTraceSection(teamName, totalPts, trace) {
    if (!trace) return '';
    const sourceLabels = {
        team:        '✓ Direkter Team-Eintrag in DVV-Liste',
        seeding:     '~ Geschätzt aus Turnier-Setzliste (kein DVV-Eintrag)',
        individuals: '∑ Summe der Einzelspieler-Punkte',
        shares:      '~ Approximiert: Anteile aus anderen Teams (50% pro Spieler)',
        missing:     '✗ Keine DVV-Daten gefunden',
    };
    const breakdownHtml = trace.breakdown.map(b => {
        if (b.type === 'team') {
            return `<li><strong>${escapeHtml(b.label)}</strong>: ${b.value} pts <span class="md-tag md-tag-good">DVV Team</span></li>`;
        }
        if (b.type === 'seeding') {
            return `<li><strong>${escapeHtml(b.label)}</strong>: ${b.value} pts <span class="md-tag md-tag-warn">~ Setzliste geschätzt</span></li>`;
        }
        if (b.type === 'individual') {
            return `<li><strong>${escapeHtml(b.label)}</strong>: ${b.value} pts <span class="md-tag">DVV Einzel</span></li>`;
        }
        if (b.type === 'share') {
            return `<li>
                <strong>${escapeHtml(b.label)}</strong>: ${b.value} pts
                <span class="md-tag md-tag-warn">~ approximiert</span>
                <div class="md-trace-detail">
                    aus „${escapeHtml(b.from || '?')}" (${b.fromPoints} pts) ÷ 2 = ${b.value}
                </div>
            </li>`;
        }
        return `<li><strong>${escapeHtml(b.label)}</strong>: ❌ nicht in DVV-Liste</li>`;
    }).join('');

    return `
    <div class="md-section">
        <div class="md-section-title">${escapeHtml(teamName)} — DVV: <strong>${totalPts} pts</strong></div>
        <div class="md-source-label">${sourceLabels[trace.source] || trace.source}</div>
        <ul class="md-trace">${breakdownHtml}</ul>
    </div>`;
}

// Rating detail for the ELO / DVV prediction trees: shows each team's rating
// and — for ELO — how the team value + win probability were computed.
function renderRatingSection(m) {
    if (bracketTree === 'dvv') {
        const a = m.ptsA ?? 0, b = m.ptsB ?? 0;
        const pct = (a + b) > 0 ? (a / (a + b) * 100).toFixed(1) : '50.0';
        return `
        <div class="md-section">
            <div class="md-section-title">DVV-Punkte</div>
            <div class="md-rating-row"><span>${escapeHtml(m.teamA)}</span><strong>${a} pts</strong></div>
            <div class="md-rating-row"><span>${escapeHtml(m.teamB)}</span><strong>${b} pts</strong></div>
            <div class="md-source-label">Siegwahrscheinlichkeit = ${a}/(${a}+${b}) = <strong>${pct}&nbsp;%</strong> für ${escapeHtml(m.teamA)}</div>
        </div>`;
    }
    if (bracketTree === 'elo') {
        const model = TREE_LABELS.elo && (_eloTreeModel || 'ensemble');
        const teamBlock = (name, elo, breakdown) => {
            if (elo == null) {
                return `<div class="md-rating-row"><span>${escapeHtml(name)}</span>
                        <em style="color:var(--warning)">kein ELO-Rating → DVV-Fallback</em></div>`;
            }
            const parts = (breakdown || [])
                .map(c => `${escapeHtml(c.name)}: ${c.rating == null ? '–' : Math.round(c.rating)}`)
                .join(' · ');
            return `<div class="md-rating-row"><span>${escapeHtml(name)}</span><strong>Ø ${Math.round(elo)}</strong></div>
                    <div class="md-trace-detail">Ø der Einzel-ELOs: ${parts}</div>`;
        };
        let formula = '';
        if (m.eloA != null && m.eloB != null) {
            const pA = (1 / (1 + Math.pow(10, (m.eloB - m.eloA) / 400)) * 100).toFixed(1);
            formula = `<div class="md-source-label">
                P(${escapeHtml(m.teamA)}) = 1 / (1 + 10^((${Math.round(m.eloB)} − ${Math.round(m.eloA)}) / 400))
                = <strong>${pA}&nbsp;%</strong></div>`;
        } else {
            formula = `<div class="md-source-label">Mind. ein Team ohne ELO-Rating — Vorhersage über DVV-Punkte-Verhältnis.</div>`;
        }
        return `
        <div class="md-section">
            <div class="md-section-title">ELO-Rating <span class="md-tag">Modell: ${escapeHtml(model)}</span></div>
            ${teamBlock(m.teamA, m.eloA, m.eloBreakA)}
            ${teamBlock(m.teamB, m.eloB, m.eloBreakB)}
            ${formula}
        </div>`;
    }
    return '';
}

function renderRefHint(m) {
    if (m.refA?.startsWith('S') && m.refB?.startsWith('S')) return '';
    const formatRef = (ref) => {
        const t = ref[0]; const n = ref.slice(1);
        if (t === 'S') return `Seed ${n}`;
        if (t === 'W') return `Gewinner Spiel ${n}`;
        if (t === 'L') return `Verlierer Spiel ${n}`;
        return ref;
    };
    return `
    <div class="md-section md-hint">
        <div class="md-section-title">Bracket-Bezug</div>
        <div>${formatRef(m.refA)} &nbsp;vs&nbsp; ${formatRef(m.refB)}</div>
    </div>`;
}

// ── Render helpers ────────────────────────────────────────────────────────────

function posColor(pos) {
    if (pos === 'Block')  return '#64ffda';
    if (pos === 'Abwehr') return '#ffd700';
    return '#a78bfa';
}

function playerCard(p, index, showPrice) {
    const ptColor = posColor(p.pos);
    const isAvail = p.price !== null && p.price > 0;
    const priceHtml = isAvail
        ? `<div class="player-price"><span class="coin-icon">₡</span>${p.price}</div>`
        : `<div class="player-price" style="color:var(--text-dim);font-size:0.78rem">nicht verf.</div>`;

    // Find this player's expectedPoints from availablePlayers (if loaded)
    const ap = availablePlayers.find(x => x.id === p.id);
    const expPts = ap?.expectedPoints ?? null;
    const isEst  = ap?.isEstimated ?? false;

    const expStat = expPts !== null
        ? `<div class="stat">
              <span class="stat-label">Erw. Pts</span>
              <span class="stat-value highlight">${expPts.toFixed(0)}</span>
           </div>`
        : '';
    const effStat = isAvail
        ? `<div class="stat">
              <span class="stat-label">Pts/₡</span>
              <span class="stat-value">${p.avgPerCoin.toFixed(2)}</span>
           </div>`
        : '';

    return `
    <div class="player-card player-card-clickable"
         onclick="openPlayerDetail('${p.id}')"
         style="animation: slideUp 0.4s ease-out ${Math.min(index, 30) * 0.03}s both;cursor:pointer"
         title="Klicken: Detail-Ansicht (Stats + Turnier-Historie)">
        <div class="player-header">
            <div>
                <div class="player-name">${p.name}</div>
                <span class="player-position" style="background:${ptColor}22;color:${ptColor}">${p.pos}</span>
                <span class="player-position" style="background:#ffffff11;color:var(--text-dim);margin-left:0.3rem">${p.gender === 'M' ? '♂' : '♀'}</span>
                ${isEst ? '<span class="player-estimated-badge" title="Statistiken geschätzt (kein historischer Wert verfügbar) — Basis: Ø gleichwertiger Spieler ±5 Coins, 75% gewichtet">~geschätzt</span>' : ''}
            </div>
            ${priceHtml}
        </div>
        <div class="player-stats">
            <div class="stat">
                <span class="stat-label">Saison-Pts</span>
                <span class="stat-value">${p.tp.toFixed(1)}</span>
            </div>
            <div class="stat">
                <span class="stat-label">Ø/Turnier</span>
                <span class="stat-value">${p.avgPerTournament.toFixed(1)}</span>
            </div>
            <div class="stat">
                <span class="stat-label">Turniere</span>
                <span class="stat-value">${p.t}</span>
            </div>
            ${effStat}
            ${expStat}
        </div>
    </div>`;
}

// ── Players tab — multi-select filters + sort ────────────────────────────────

const playerFilters = {
    pos:      new Set(['Block', 'Abwehr', 'Hybrid']),
    gender:   new Set(['M', 'W']),
    status:   new Set(),     // 'available' toggle
    sortBy:   'tp',
    priceMin: null,           // null = no lower bound
    priceMax: null,           // null = no upper bound
};

// Returns the filter-bar HTML for the given tab prefix (so two copies — one
// in the players tab, one in the picks tab — don't clash on element IDs).
// Reads from `playerFilters` so both bars stay in sync after a re-render.
function renderFilterBarHTML(prefix) {
    const pf = playerFilters;
    const ck = (g, v) => pf[g].has(v) ? 'checked' : '';
    // The collapsible wrapper hides position/gender/status/price on mobile by default;
    // the .player-filters-toggle button toggles a `.filters-open` class that reveals it.
    return `
    <div class="player-filters">
        <button type="button" class="player-filters-toggle" onclick="togglePlayerFilters(this)"
                aria-expanded="false">🔎 Filter ▾</button>
        <div class="player-filters-collapsible">
            <div class="filter-group">
                <span class="filter-label">Position</span>
                <div class="filter-checks">
                    <label class="filter-check"><input type="checkbox" data-group="pos" data-val="Block"  ${ck('pos','Block')}> Block</label>
                    <label class="filter-check"><input type="checkbox" data-group="pos" data-val="Abwehr" ${ck('pos','Abwehr')}> Abwehr</label>
                    <label class="filter-check"><input type="checkbox" data-group="pos" data-val="Hybrid" ${ck('pos','Hybrid')}> Hybrid</label>
                </div>
            </div>
            <div class="filter-group">
                <span class="filter-label">Geschlecht</span>
                <div class="filter-checks">
                    <label class="filter-check"><input type="checkbox" data-group="gender" data-val="M" ${ck('gender','M')}> ♂ Männer</label>
                    <label class="filter-check"><input type="checkbox" data-group="gender" data-val="W" ${ck('gender','W')}> ♀ Frauen</label>
                </div>
            </div>
            <div class="filter-group">
                <span class="filter-label">Status</span>
                <div class="filter-checks">
                    <label class="filter-check"><input type="checkbox" data-group="status" data-val="available" ${ck('status','available')}> Nur verfügbar</label>
                </div>
            </div>
            <div class="filter-group">
                <span class="filter-label">Preis (₡)</span>
                <div class="filter-price">
                    <input type="number" class="filter-price-input" data-price-edge="min" placeholder="min" min="0"
                           value="${pf.priceMin ?? ''}">
                    <span class="filter-price-sep">–</span>
                    <input type="number" class="filter-price-input" data-price-edge="max" placeholder="max" min="0"
                           value="${pf.priceMax ?? ''}">
                </div>
            </div>
        </div>
        <div class="filter-group filter-group-sort">
            <span class="filter-label">Sortieren nach</span>
            <select class="filter-sort" data-sort-select>
                <option value="tp"               ${pf.sortBy==='tp'?'selected':''}>Saison-Punkte ↓</option>
                <option value="avgPerTournament" ${pf.sortBy==='avgPerTournament'?'selected':''}>Ø Punkte/Turnier ↓</option>
                <option value="avgPerCoin"       ${pf.sortBy==='avgPerCoin'?'selected':''}>Effizienz (Pts/Coin) ↓</option>
                <option value="expectedPoints"   ${pf.sortBy==='expectedPoints'?'selected':''}>Erw. Turnier-Punkte ↓</option>
                <option value="price"            ${pf.sortBy==='price'?'selected':''}>Preis ↓</option>
                <option value="t"                ${pf.sortBy==='t'?'selected':''}>Anzahl Turniere ↓</option>
            </select>
        </div>
    </div>`;
}

// Mobile: toggle the controls (settings) bar.
function toggleSettings() {
    const controls = document.querySelector('.controls');
    if (!controls) return;
    const open = controls.classList.toggle('settings-open');
    const btn  = document.getElementById('settingsToggleBtn');
    if (btn) {
        btn.setAttribute('aria-expanded', String(open));
        btn.textContent = open ? '⚙ Einstellungen ▴' : '⚙ Einstellungen ▾';
    }
}

// Mobile: toggle the collapsible block of the player filters bar.
function togglePlayerFilters(btn) {
    const bar = btn.closest('.player-filters');
    if (!bar) return;
    const open = bar.classList.toggle('filters-open');
    btn.setAttribute('aria-expanded', String(open));
    btn.textContent = open ? '🔎 Filter ▴' : '🔎 Filter ▾';
}

// Re-renders whichever tab the user is currently on (both bars share state).
function rerenderActiveFilteredTab() {
    if (document.getElementById('playersTab')?.classList.contains('active')) renderPlayers();
    if (document.getElementById('picksTab')?.classList.contains('active'))   renderPicksTab();
}

// Returns true if player passes the current filter set (used by both tabs).
function passesPlayerFilters(p, { skipStatus = false } = {}) {
    if (!playerFilters.pos.has(p.pos)) return false;
    if (!playerFilters.gender.has(p.gender)) return false;
    if (!skipStatus && playerFilters.status.has('available') && !(p.price !== null && p.price > 0)) return false;
    if (playerFilters.priceMin !== null && (p.price ?? -1) < playerFilters.priceMin) return false;
    if (playerFilters.priceMax !== null && (p.price ?? Infinity) > playerFilters.priceMax) return false;
    return true;
}

function onFilterChange(e) {
    const cb = e.target;
    const group = cb.dataset.group;
    const val   = cb.dataset.val;
    if (!group || !val) return;
    const set = playerFilters[group];
    if (cb.checked) set.add(val); else set.delete(val);
    // Mirror to all other filter bars currently in the DOM
    document.querySelectorAll(
        `.filter-check input[data-group="${group}"][data-val="${val}"]`
    ).forEach(other => { if (other !== cb) other.checked = cb.checked; });
    rerenderActiveFilteredTab();
}

function onSortChange(e) {
    playerFilters.sortBy = e.target.value;
    document.querySelectorAll('select[data-sort-select]').forEach(s => {
        if (s !== e.target) s.value = e.target.value;
    });
    rerenderActiveFilteredTab();
}

function onPriceFilterChange(e) {
    const edge = e.target.dataset.priceEdge;
    if (!edge) return;
    const raw = e.target.value.trim();
    const val = raw === '' ? null : Math.max(0, parseInt(raw, 10) || 0);
    playerFilters[edge === 'min' ? 'priceMin' : 'priceMax'] = val;
    document.querySelectorAll(`input.filter-price-input[data-price-edge="${edge}"]`).forEach(o => {
        if (o !== e.target) o.value = raw;
    });
    rerenderActiveFilteredTab();
}

function renderPlayers() {
    const tab = document.getElementById('playersTab');
    // Re-render the filter bar from current state (keeps inputs in sync after
    // changes from the picks tab's mirror bar). Always rebuild so the bar's
    // HTML matches renderFilterBarHTML — important when index.html's static
    // markup is missing newer controls like the price-range filter.
    const existingBar = tab.querySelector('.player-filters');
    // Don't blow away focus while the user is mid-typing in a price box
    const focusedEdge = document.activeElement?.matches?.('#playersTab .filter-price-input')
        ? document.activeElement.dataset.priceEdge : null;
    if (existingBar) {
        existingBar.outerHTML = renderFilterBarHTML('players');
    } else {
        tab.insertAdjacentHTML('afterbegin', renderFilterBarHTML('players'));
    }
    if (focusedEdge) {
        const el = tab.querySelector(`.filter-price-input[data-price-edge="${focusedEdge}"]`);
        if (el) { el.focus(); el.setSelectionRange(el.value.length, el.value.length); }
    }

    const grid = document.getElementById('playerGrid');

    let list = allPlayers.filter(p => passesPlayerFilters(p));

    // Pull simulated metrics into allPlayers (only available players have these)
    const expByName = {};
    availablePlayers.forEach(p => {
        if (p.expectedPoints !== null && p.expectedPoints !== undefined) {
            expByName[p.id] = p.expectedPoints;
        }
    });

    const sortKey = playerFilters.sortBy;
    const valueFor = (p) => {
        if (sortKey === 'expectedPoints') return expByName[p.id] ?? -1;
        if (sortKey === 'avgPerCoin')     return p.avgPerCoin || 0;
        if (sortKey === 'price')          return p.price ?? -1;
        return p[sortKey] ?? 0;
    };
    list.sort((a, b) => valueFor(b) - valueFor(a));

    if (list.length === 0) {
        grid.innerHTML = '<div class="no-results">Keine Spieler — Filter zu eng?</div>';
        return;
    }

    grid.innerHTML = list.map((p, i) => playerCard(p, i, true)).join('');
}

// ── Picks tab — locked (pre-selected) players ─────────────────────────────────

function saveLocked() {
    localStorage.setItem('lockedPlayerIds', JSON.stringify([...lockedPlayerIds]));
    localStorage.setItem('bannedPlayerIds', JSON.stringify([...bannedPlayerIds]));
}

function toggleLock(id) {
    if (lockedPlayerIds.has(id)) {
        lockedPlayerIds.delete(id);
    } else {
        bannedPlayerIds.delete(id);   // can't be locked + banned
        lockedPlayerIds.add(id);
    }
    saveLocked();
    renderPicksTab();
}

function toggleBan(id, evt) {
    evt.stopPropagation();            // don't trigger card click (lock)
    if (bannedPlayerIds.has(id)) {
        bannedPlayerIds.delete(id);
    } else {
        lockedPlayerIds.delete(id);   // can't be banned + locked
        bannedPlayerIds.add(id);
    }
    saveLocked();
    renderPicksTab();
}

function clearLocked() {
    lockedPlayerIds.clear();
    bannedPlayerIds.clear();
    saveLocked();
    renderPicksTab();
}

function renderPicksTab() {
    const budget   = parseInt(document.getElementById('budget').value)   || 240;
    const teamSize = parseInt(document.getElementById('teamSize').value) || 6;

    // Picks tab is inherently about priced/available players, so skip the
    // status check (everything in `pool` already satisfies price > 0).
    const pool   = availablePlayers.filter(p => p.price > 0);
    const lockedAll = pool.filter(p => lockedPlayerIds.has(p.id));
    const bannedAll = pool.filter(p => bannedPlayerIds.has(p.id));
    const normalAll = pool.filter(p => !lockedPlayerIds.has(p.id) && !bannedPlayerIds.has(p.id));

    // Sort using the filter bar's sortBy
    const expByName = {};
    pool.forEach(p => { if (p.expectedPoints != null) expByName[p.id] = p.expectedPoints; });
    const sortKey = playerFilters.sortBy;
    const valueFor = (p) => {
        if (sortKey === 'expectedPoints') return expByName[p.id] ?? -1;
        if (sortKey === 'avgPerCoin')     return p.avgPerCoin || 0;
        if (sortKey === 'price')          return p.price ?? -1;
        return p[sortKey] ?? 0;
    };
    const sortAndFilter = (arr) =>
        arr.filter(p => passesPlayerFilters(p, { skipStatus: true }))
           .sort((a, b) => valueFor(b) - valueFor(a));

    // Status counts shown in the header always reflect the FULL pick state
    // (filters don't reduce what's actually locked/banned), but the displayed
    // grid cards are filtered.
    const locked = sortAndFilter(lockedAll);
    const banned = sortAndFilter(bannedAll);
    const normal = sortAndFilter(normalAll);
    // Keep variable names below pointing at the unfiltered counts for the status row
    const lockedCount = lockedAll.length;
    const bannedCount = bannedAll.length;

    // Budget / status math uses the UNFILTERED lock state (filters only affect
    // which cards are displayed, not what's actually locked/banned).
    const lockedCost = lockedAll.reduce((s, p) => s + p.price, 0);
    const remBudget  = budget - lockedCost;
    const remSlots   = teamSize - lockedCount;
    const hasPicks   = lockedCount > 0 || bannedCount > 0;
    const over       = remBudget < 0 || remSlots < 0;
    const totalFiltered = locked.length + banned.length + normal.length;
    const hasActiveFilter = totalFiltered < pool.length;

    const statusHtml = `
        ${renderFilterBarHTML('picks')}
        <div class="${over ? 'picks-status picks-status-over' : 'picks-status'}">
            <span>🔒 ${lockedCount} fest</span>
            <span>🚫 ${bannedCount} ausgeschlossen</span>
            <span>₡ ${lockedCost} / ${budget} verbraucht</span>
            <span class="${over ? 'picks-over' : ''}">${over ? '⚠ ' : ''}₡ ${remBudget} übrig</span>
            <span class="${remSlots < 0 ? 'picks-over' : ''}">${remSlots} Slot${remSlots !== 1 ? 's' : ''} offen</span>
            ${hasActiveFilter ? `<span class="picks-filter-note">🔍 ${totalFiltered}/${pool.length} Spieler sichtbar</span>` : ''}
            ${hasPicks ? `<button class="btn-inline btn-inline-ghost" onclick="clearLocked()" style="font-size:0.8rem;padding:0.2rem 0.6rem">🗑 Alles zurücksetzen</button>` : ''}
        </div>
        <p class="tab-hint" style="margin-top:0;margin-bottom:1.2rem">
            <strong>Kachel klicken</strong> = Spieler/in fest ins Team sperren (🔒) ·
            <strong>🚫-Button</strong> = Vom Algorithmus ausschließen
        </p>`;

    const lockedSection = locked.length === 0 ? '' : `
        <div class="picks-section-title">🔒 Fest gesperrt — immer im Team${lockedCount !== locked.length ? ` <span class="picks-filter-note">(${locked.length}/${lockedCount} angezeigt)</span>` : ''}</div>
        <div class="player-grid picks-locked-grid">
            ${locked.map((p, i) => picksCard(p, i, 'locked')).join('')}
        </div>`;

    const bannedSection = banned.length === 0 ? '' : `
        <div class="picks-section-title" style="margin-top:1.2rem">🚫 Ausgeschlossen — nie vom Algorithmus gepickt${bannedCount !== banned.length ? ` <span class="picks-filter-note">(${banned.length}/${bannedCount} angezeigt)</span>` : ''}</div>
        <div class="player-grid picks-banned-grid">
            ${banned.map((p, i) => picksCard(p, i, 'banned')).join('')}
        </div>`;

    const normalSection = `
        <div class="picks-section-title" style="margin-top:${hasPicks ? '1.5rem' : '0'}">
            Verfügbare Spieler
        </div>
        <div class="player-grid">
            ${normal.length === 0
                ? `<div class="no-results">${normalAll.length === 0 ? 'Keine weiteren verfügbaren Spieler.' : 'Filter zu eng — keine Treffer.'}</div>`
                : normal.map((p, i) => picksCard(p, i, 'normal')).join('')}
        </div>`;

    // Preserve focus on the price-range inputs across the full innerHTML rebuild,
    // so the user can keep typing without losing their cursor every keystroke.
    const focusedEdge = document.activeElement?.matches?.('#picksTab .filter-price-input')
        ? document.activeElement.dataset.priceEdge : null;
    document.getElementById('picksTab').innerHTML =
        statusHtml + lockedSection + bannedSection + normalSection;
    if (focusedEdge) {
        const el = document.querySelector(`#picksTab .filter-price-input[data-price-edge="${focusedEdge}"]`);
        if (el) { el.focus(); el.setSelectionRange(el.value.length, el.value.length); }
    }
}

// state: 'locked' | 'banned' | 'normal'
function picksCard(p, index, state) {
    const ptColor  = posColor(p.pos);
    const expPts   = p.expectedPoints ?? null;
    const isEst    = p.isEstimated ?? false;
    const isLocked = state === 'locked';
    const isBanned = state === 'banned';

    // Card click = toggle lock (or unban if banned)
    const cardClick = isBanned
        ? `onclick="toggleBan('${p.id}', event)"`
        : `onclick="toggleLock('${p.id}')"`;
    const cardCls = isLocked ? 'picks-card-locked' : isBanned ? 'picks-card-banned' : 'picks-card-normal';

    // Right-side button: ban toggle (stop-propagation so card click doesn't also fire)
    const banBtn = isLocked ? '' :
        isBanned
            ? `<button class="picks-btn picks-btn-banned" onclick="toggleBan('${p.id}', event)" title="Freigeben">✓ Freigeben</button>`
            : `<button class="picks-btn picks-btn-ban"    onclick="toggleBan('${p.id}', event)" title="Ausschließen">🚫</button>`;

    const lockIndicator = isLocked
        ? '<span class="picks-lock-indicator" title="Fest gesperrt — klicken zum Freigeben">🔒</span>'
        : isBanned
        ? '<span class="picks-lock-indicator" title="Ausgeschlossen — klicken zum Freigeben">🚫</span>'
        : '<span class="picks-lock-indicator picks-lock-hint">🔒</span>';

    return `
    <div class="player-card ${cardCls} picks-card-clickable"
         ${cardClick}
         style="animation:slideUp 0.3s ease-out ${Math.min(index,30)*0.02}s both;cursor:pointer"
         title="${isLocked ? 'Klicken zum Freigeben' : isBanned ? 'Klicken zum Freigeben' : 'Klicken zum Sperren'}">
        <div class="player-header">
            <div style="display:flex;align-items:center;gap:0.5rem">
                ${lockIndicator}
                <div>
                    <div class="player-name">${p.name}</div>
                    <span class="player-position" style="background:${ptColor}22;color:${ptColor}">${p.pos}</span>
                    <span class="player-position" style="background:#ffffff11;color:var(--text-dim);margin-left:0.3rem">${p.gender === 'M' ? '♂' : '♀'}</span>
                    ${isEst ? '<span class="player-estimated-badge">~geschätzt</span>' : ''}
                </div>
            </div>
            <div style="display:flex;flex-direction:column;align-items:flex-end;gap:0.35rem">
                <div class="player-price"><span class="coin-icon">₡</span>${p.price}</div>
                ${banBtn}
            </div>
        </div>
        <div class="player-stats">
            <div class="stat"><span class="stat-label">Saison-Pts</span><span class="stat-value">${p.tp.toFixed(1)}</span></div>
            <div class="stat"><span class="stat-label">Ø/Turnier</span><span class="stat-value">${p.avgPerTournament.toFixed(1)}</span></div>
            <div class="stat"><span class="stat-label">Turniere</span><span class="stat-value">${p.t}</span></div>
            <div class="stat"><span class="stat-label">Pts/₡</span><span class="stat-value">${p.avgPerCoin.toFixed(2)}</span></div>
            ${expPts !== null ? `<div class="stat"><span class="stat-label">Erw. Pts</span><span class="stat-value highlight">${expPts.toFixed(0)}</span></div>` : ''}
        </div>
    </div>`;
}

// ── Optimization algorithms ───────────────────────────────────────────────────

function getObjectiveValue(player, alg) {
    if (alg === 'consistent') {
        // Prefer real variance-based score from per-tournament history; fall back
        // to Bayesian shrinkage if not enough history (<3 tournaments).
        return player.varianceScore ?? player.adjustedPT ?? player.avgPerTournament;
    }
    if (alg === 'form-trend') return player.formScore ?? player.avgPerTournament;
    // Per-tree bracket algorithms: prog-<tree> (expected points from that tree),
    // final-<tree> (round level × 1000 + season avg as tiebreaker).
    if (alg.startsWith('prog-')) {
        const t = alg.slice(5);
        return player.treeExpPoints?.[t] ?? player.avgPerTournament;
    }
    if (alg.startsWith('final-')) {
        const t = alg.slice(6);
        return (player.treeRound?.[t] ?? 0) * 1000 + player.avgPerTournament;
    }
    return player.avgPerTournament;
}

// Compute Bayesian-adjusted pts/tournament for each player.
// Shrinks towards the pool mean — players with few tournaments are penalized.
function computeAdjustedPT(candidates, k = 3) {
    const active = candidates.filter(p => p.t > 0);
    if (active.length === 0) return;
    const priorMean = active.reduce((s, p) => s + p.avgPerTournament, 0) / active.length;
    candidates.forEach(p => {
        p.adjustedPT = p.t > 0
            ? (p.t * p.avgPerTournament + k * priorMean) / (p.t + k)
            : 0;
    });
}

// Variance-penalized score: mean − λ × stdDev of per-tournament fantasyPoints.
// Needs ≥3 tournaments in playerHistory to be meaningful; falls back to adjustedPT
// (Bayesian shrinkage) otherwise so the Konsistent algorithm degrades gracefully.
function computeVarianceScore(candidates, lambda = 0.5) {
    const hist = window.playerHistory || {};
    candidates.forEach(p => {
        const entries = hist[p.id];
        if (!entries || entries.length < 3) {
            p.varianceScore = null;   // signal: fall back
            return;
        }
        const fps = entries.map(e => e.fantasyPoints).filter(v => typeof v === 'number');
        if (fps.length < 3) { p.varianceScore = null; return; }
        const mean = fps.reduce((s, v) => s + v, 0) / fps.length;
        const variance = fps.reduce((s, v) => s + (v - mean) ** 2, 0) / fps.length;
        p.varianceScore = mean - lambda * Math.sqrt(variance);
    });
}

// Form-trend score: weighted average of the last 3 tournament fantasy points,
// weights [0.5, 0.3, 0.2] (newest first). Null if fewer than 3 entries.
function computeFormScore(candidates) {
    const hist = window.playerHistory || {};
    const WEIGHTS = [0.5, 0.3, 0.2];
    candidates.forEach(p => {
        const entries = hist[p.id];
        if (!entries || entries.length < 3) { p.formScore = null; return; }
        // Entries are chronological (oldest first) — take last 3 reversed (newest first).
        const recent = entries.slice(-3).reverse();
        let score = 0;
        for (let i = 0; i < WEIGHTS.length; i++) {
            score += WEIGHTS[i] * (recent[i].fantasyPoints ?? 0);
        }
        p.formScore = score;
    });
}

// Side-out indicators aggregated across all tournaments in player_history.
// receptionRate = good / (good + bad + error)
// attackEfficiency = (kills − error − blocked) / (kills + error + blocked + bad)
// Stored on the player object for display in modals / H2H; NOT used as algorithm objective.
function computeSideOutMetrics(candidates) {
    const hist = window.playerHistory || {};
    candidates.forEach(p => {
        const entries = hist[p.id] || [];
        let rG = 0, rB = 0, rE = 0;
        let aK = 0, aErr = 0, aBlk = 0, aBad = 0;
        for (const e of entries) {
            rG += e.receptionGood  ?? 0;
            rB += e.receptionBad   ?? 0;
            rE += e.receptionError ?? 0;
            aK   += e.attackKills   ?? 0;
            aErr += e.attackError   ?? 0;
            aBlk += e.attackBlocked ?? 0;
            aBad += e.attackBad     ?? 0;
        }
        const recTot = rG + rB + rE;
        const atkTot = aK + aErr + aBlk + aBad;
        p.receptionRate    = recTot > 0 ? rG / recTot : null;
        p.attackEfficiency = atkTot > 0 ? (aK - aErr - aBlk) / atkTot : null;
    });
}

// Branch & Bound – exact optimal solution accounting for the captain bonus.
// Team score = Σ v(p) + 0.5 × max(v(p))  [captain scores 1.5× their value]
// captainVal tracks the current max player value in the team; the bonus on top is 0.5 × captainVal.
// Upper bound = current_total + fractional_remaining_sum + max_possible_extra_captain.
function optimizeBranchBound(candidates, budget, teamSize, maxBlock, maxAbwehr, alg,
                              minMen = 0, minWomen = 0, exactGender = false) {
    const sorted = [...candidates]
        .filter(p => getObjectiveValue(p, alg) > 0)
        .sort((a, b) =>
            getObjectiveValue(b, alg) / b.price - getObjectiveValue(a, alg) / a.price
        );

    const n = sorted.length;
    let bestValue = -Infinity;
    let bestTeam  = [];

    // Global max value (upper bound on any player's captain bonus contribution)
    const globalMaxVal = n > 0
        ? Math.max(...sorted.map(p => getObjectiveValue(p, alg)))
        : 0;

    // Gender constraint active at all?
    const genderActive = minMen > 0 || minWomen > 0;

    // Fractional knapsack upper bound on remaining player sum
    function sumUpperBound(fromIdx, remBudget, remSlots) {
        let value = 0, bud = remBudget, slots = remSlots;
        for (let i = fromIdx; i < n && slots > 0 && bud > 0; i++) {
            const p = sorted[i];
            const v = getObjectiveValue(p, alg);
            if (p.price <= bud) {
                value += v;
                bud   -= p.price;
                slots--;
            } else {
                value += v * (bud / p.price);
                break;
            }
        }
        return value;
    }

    // captainVal = max objective value in current team; bonus on top of sumVal = 0.5 × captainVal
    function dfs(idx, team, cost, sumVal, captainVal, bc, ac, mc, wc) {
        const slotsLeft = teamSize - team.length;
        const totalVal  = sumVal + 0.5 * captainVal;

        if (slotsLeft === 0 || idx >= n || n - idx < slotsLeft) {
            // At a leaf, only accept solution if gender minimums are met
            if (slotsLeft === 0 && genderActive && (mc < minMen || wc < minWomen)) return;
            if (totalVal > bestValue) { bestValue = totalVal; bestTeam = [...team]; }
            return;
        }

        // Gender-feasibility pruning: can we still reach the minimums with remaining slots?
        if (genderActive) {
            const need = Math.max(0, minMen - mc) + Math.max(0, minWomen - wc);
            if (need > slotsLeft) return;
        }

        // Extra captain bonus we could gain from remaining players (1.5× → +0.5× of delta)
        const extraCaptainBound = 0.5 * Math.max(0, globalMaxVal - captainVal);
        if (totalVal + sumUpperBound(idx, budget - cost, slotsLeft) + extraCaptainBound <= bestValue) return;

        for (let i = idx; i <= n - slotsLeft; i++) {
            const p = sorted[i];
            const v = getObjectiveValue(p, alg);

            if (cost + p.price > budget) continue;
            if (p.pos === 'Block'  && maxBlock  > 0 && bc >= maxBlock)  continue;
            if (p.pos === 'Abwehr' && maxAbwehr > 0 && ac >= maxAbwehr) continue;
            // Exact-gender cap: if the user fully partitions the team by gender,
            // the residual minMen/minWomen are also the hard maximum for this DFS run.
            // (No `> 0` guard — when remMinMen is 0 because locked already covers the
            // quota, every additional man must still be skipped.)
            if (exactGender) {
                if (p.gender === 'M' && mc >= minMen)   continue;
                if (p.gender === 'W' && wc >= minWomen) continue;
            }

            const newCaptainVal = Math.max(captainVal, v);
            const newSumVal     = sumVal + v;
            const newTotalVal   = newSumVal + 0.5 * newCaptainVal;
            const newExtraBound = 0.5 * Math.max(0, globalMaxVal - newCaptainVal);

            if (newTotalVal + sumUpperBound(i + 1, budget - cost - p.price, slotsLeft - 1) + newExtraBound <= bestValue) continue;

            team.push(p);
            dfs(i + 1, team, cost + p.price, newSumVal, newCaptainVal,
                bc + (p.pos === 'Block'  ? 1 : 0),
                ac + (p.pos === 'Abwehr' ? 1 : 0),
                mc + (p.gender === 'M' ? 1 : 0),
                wc + (p.gender === 'W' ? 1 : 0));
            team.pop();
        }
    }

    dfs(0, [], 0, 0, 0, 0, 0, 0, 0);
    return bestTeam;
}

// ── Main optimize entry point ─────────────────────────────────────────────────

function optimizeTeam() {
    // There can be two Optimize buttons (mobile-primary + desktop) — toggle both
    // so the spinner state stays consistent regardless of which one was clicked.
    const btns = Array.from(document.querySelectorAll('button[onclick="optimizeTeam()"]'));
    const btnLabels = btns.map(b => b.textContent);
    const setBtnState = (disabled, text) => {
        btns.forEach((b, i) => {
            b.disabled = disabled;
            b.textContent = text !== null ? text : btnLabels[i];
        });
    };

    try {
        const budget    = parseInt(document.getElementById('budget').value);
        const teamSize  = parseInt(document.getElementById('teamSize').value);
        const maxBlock  = parseInt(document.getElementById('maxBlock').value);
        const maxAbwehr = parseInt(document.getElementById('maxAbwehr').value);
        const minMen    = parseInt(document.getElementById('minMen').value)   || 0;
        const minWomen  = parseInt(document.getElementById('minWomen').value) || 0;

        if (minMen + minWomen > teamSize) {
            alert('Anzahl Männer + Frauen (' + (minMen + minWomen) + ') überschreitet die Teamgröße ('
                  + teamSize + ').\n\nBitte Werte anpassen — Summe muss ≤ Teamgröße sein. '
                  + 'Wenn die Summe genau der Teamgröße entspricht, wird die Aufteilung exakt erzwungen; '
                  + 'wenn kleiner, gilt sie als Mindestanzahl.');
            return;
        }

        if (!availablePlayers || availablePlayers.length === 0) {
            alert('Keine verfügbaren Spieler geladen.\n\nFalls die Seite gerade lädt, einen Moment warten und nochmal klicken.');
            return;
        }

        setBtnState(true, '⏳ Berechne…');

        // Defer heavy work so the spinner paints, but wrap in try/finally so
        // any throw can't leave the button disabled and the page silently dead.
        setTimeout(async () => {
            try {
                // Make sure the selected ELO model is loaded so the ELO tree can
                // be built (best-effort — the ELO panels are skipped if it fails).
                await ensureEloTreeData().catch(() => {});
                runOptimizePipeline(budget, teamSize, maxBlock, maxAbwehr, minMen, minWomen);
                renderCompare();
                switchToTab('compare');
            } catch (err) {
                console.error('optimizeTeam (deferred) failed:', err);
                alert('Beim Optimieren ist ein Fehler aufgetreten:\n\n'
                      + (err?.message || String(err))
                      + '\n\nDetails in der Browser-Konsole (F12).');
            } finally {
                setBtnState(false, null);
            }
        }, 20);
    } catch (err) {
        // Anything thrown by the SYNCHRONOUS portion lands here (e.g. missing
        // input element, etc.) — restore the button so the page isn't frozen.
        console.error('optimizeTeam (sync) failed:', err);
        alert('optimizeTeam: ' + (err?.message || String(err)));
        setBtnState(false, null);
    }
}

function runOptimizePipeline(budget, teamSize, maxBlock, maxAbwehr, minMen = 0, minWomen = 0) {
    computeAdjustedPT(availablePlayers);
    // History-based metrics — no-op when player_history.json is missing.
    computeVarianceScore(availablePlayers);
    computeFormScore(availablePlayers);
    computeSideOutMetrics(availablePlayers);

    // Picks (locked) and bans always apply to every algorithm.
    const locked = availablePlayers.filter(p => lockedPlayerIds.has(p.id) && p.price > 0);
    const lockedCost = locked.reduce((s, p) => s + p.price, 0);
    const remBudget  = budget - lockedCost;
    const remSize    = teamSize - locked.length;
    const lockedBlock  = locked.filter(p => p.pos === 'Block').length;
    const lockedAbwehr = locked.filter(p => p.pos === 'Abwehr').length;
    const lockedMen    = locked.filter(p => p.gender === 'M').length;
    const lockedWomen  = locked.filter(p => p.gender === 'W').length;
    const remMaxBlock  = maxBlock  > 0 ? Math.max(0, maxBlock  - lockedBlock)  : maxBlock;
    const remMaxAbwehr = maxAbwehr > 0 ? Math.max(0, maxAbwehr - lockedAbwehr) : maxAbwehr;
    const remMinMen    = minMen   > 0 ? Math.max(0, minMen   - lockedMen)   : 0;
    const remMinWomen  = minWomen > 0 ? Math.max(0, minWomen - lockedWomen) : 0;
    // Exact-gender mode: the user fully partitioned the full team between M and W.
    // Note: derived from the ORIGINAL teamSize/minMen/minWomen so locked players
    // already counted toward the minimums don't accidentally flip mode.
    const exactGender  = (minMen + minWomen === teamSize) && (minMen > 0 || minWomen > 0);
    // Pool excludes locked (pre-seeded) and banned (must-not-pick) players
    const pool = availablePlayers.filter(p =>
        !locked.includes(p) &&
        !bannedPlayerIds.has(p.id)
    );

    // Run all algorithms — history-based form-trend only if at least one player
    // has ≥3 tournaments; bracket-based ones once per available prediction tree.
    const algsToRun = ['optimal', 'consistent'];
    const hasFormData = availablePlayers.some(p => p.formScore != null);
    if (hasFormData) algsToRun.push('form-trend');

    // Per-tree bracket algorithms: for each available tree (current/dvv/elo/
    // personal) compute deterministic expected matches → Turnier-Prognose and
    // round levels → Finale-Fokus, stored per-tree on each player.
    availablePlayers.forEach(p => { p.treeExpPoints = {}; p.treeRound = {}; });
    const trees = availableTrees(_eloTreeIndex);
    for (const tree of trees) {
        const em = expectedMatchesFor(tree, _eloTreeIndex);
        const rl = roundLevelsFor(tree, _eloTreeIndex);
        availablePlayers.forEach(p => {
            const m = em[p.id];
            p.treeExpPoints[tree] = (m != null && p.avgPerMatch > 0) ? p.avgPerMatch * m : null;
            p.treeRound[tree]     = rl[p.id] ?? 0;
        });
        algsToRun.push(`prog-${tree}`);
        algsToRun.push(`final-${tree}`);
    }

    comparisonResults = {};
    for (const a of algsToRun) {
        let team;
        if (remSize <= 0 || remBudget < 0) {
            // Locked players already fill/exceed the team — use them directly
            team = locked;
        } else {
            const optimized = optimizeBranchBound(pool, remBudget, remSize, remMaxBlock, remMaxAbwehr, a,
                                                  remMinMen, remMinWomen, exactGender);
            team = [...locked, ...optimized];
        }
        const summary = buildTeamSummary(team, a);
        summary.lockedCount = locked.length;
        comparisonResults[a] = summary;
    }
}

// ── Team summary helpers ──────────────────────────────────────────────────────

// Returns the optimal captain for a team (player with highest objective value).
function getCaptain(team, alg) {
    if (team.length === 0) return null;
    return team.reduce((best, p) =>
        getObjectiveValue(p, alg) > getObjectiveValue(best, alg) ? p : best
    );
}

function buildTeamSummary(team, alg) {
    const captain = getCaptain(team, alg);

    // Which kind of bracket algorithm (if any) is this, and for which tree?
    const isProg  = alg.startsWith('prog-');
    const isFinal = alg.startsWith('final-');
    const tree    = isProg ? alg.slice(5) : isFinal ? alg.slice(6) : null;

    // Base sums (without captain doubling)
    const totalCost = team.reduce((s, p) => s + p.price, 0);
    const totalTP   = team.reduce((s, p) => s + p.tp, 0);
    const totalPT   = team.reduce((s, p) => s + p.avgPerTournament, 0);
    const totalAdj  = team.reduce((s, p) => s + (p.adjustedPT ?? p.avgPerTournament), 0);
    // Expected points for this tree (only meaningful for prog-<tree> panels).
    const expOf     = (p) => (isProg ? (p.treeExpPoints?.[tree] ?? 0) : 0);
    const totalExp  = team.reduce((s, p) => s + expOf(p), 0);

    // With captain bonus (= base + 0.5 × captain's value, since they score 1.5×)
    const captainPT     = captain?.avgPerTournament ?? 0;
    const totalPTCaptain  = totalPT  + 0.5 * captainPT;
    const totalExpCaptain = totalExp + 0.5 * expOf(captain ?? {});

    const blockCount  = team.filter(p => p.pos === 'Block').length;
    const abwehrCount = team.filter(p => p.pos === 'Abwehr').length;
    const hybridCount = team.filter(p => p.pos === 'Hybrid').length;

    // For final-<tree>: count players reaching semi-final (≥1) and final (≥2).
    const roundOf        = (p) => (isFinal ? (p.treeRound?.[tree] ?? 0) : 0);
    const semiFinalCount = team.filter(p => roundOf(p) >= 1).length;
    const finalCount     = team.filter(p => roundOf(p) >= 2).length;

    return {
        players: team, alg, tree, isProg, isFinal, captainId: captain?.id ?? null,
        totalCost, totalTP,
        totalPT, totalPTCaptain,
        totalAdj,
        totalExp, totalExpCaptain,
        blockCount, abwehrCount, hybridCount,
        semiFinalCount, finalCount,
    };
}

// Static (non-tree) algorithms. Per-tree bracket algorithms (prog-<tree> /
// final-<tree>) get their labels from algLabel() instead.
const ALG_LABELS = {
    optimal:      { name: 'Optimal',    icon: '⭐', desc: 'max Σ Ø/Turnier'           },
    consistent:   { name: 'Konsistent', icon: '🛡',  desc: 'Ø − λ·Streuung'            },
    'form-trend': { name: 'Form-Trend', icon: '📈', desc: 'Letzte 3 Turniere stärker' },
};

// ── Compare tab ───────────────────────────────────────────────────────────────

// Panel order + hidden set (persisted). Users drag to reorder and can hide
// panels they don't care about; new/unknown alg-ids are appended in run order.
let _compareOrder  = (() => { try { return JSON.parse(localStorage.getItem('compareOrder')  || '[]'); } catch { return []; } })();
let _compareHidden = new Set((() => { try { return JSON.parse(localStorage.getItem('compareHidden') || '[]'); } catch { return []; } })());
let _cmpDragAlg = null;
function _saveCompareOrder()  { localStorage.setItem('compareOrder',  JSON.stringify(_compareOrder)); }
function _saveCompareHidden() { localStorage.setItem('compareHidden', JSON.stringify([..._compareHidden])); }
function _orderedAlgs(algs) {
    const set = new Set(algs);
    const ordered = _compareOrder.filter(a => set.has(a));
    for (const a of algs) if (!ordered.includes(a)) ordered.push(a);
    return ordered;
}
function cmpDragStart(ev, alg) { _cmpDragAlg = alg; ev.dataTransfer.effectAllowed = 'move'; }
function cmpDragOver(ev)       { ev.preventDefault(); ev.dataTransfer.dropEffect = 'move'; }
function cmpDrop(ev, targetAlg) {
    ev.preventDefault();
    if (!_cmpDragAlg || _cmpDragAlg === targetAlg) { _cmpDragAlg = null; return; }
    let order = _orderedAlgs(Object.keys(comparisonResults)).filter(a => a !== _cmpDragAlg);
    const idx = order.indexOf(targetAlg);
    order.splice(idx < 0 ? order.length : idx, 0, _cmpDragAlg);
    _compareOrder = order; _saveCompareOrder(); _cmpDragAlg = null;
    renderCompare();
}
function cmpHidePanel(alg) { _compareHidden.add(alg);    _saveCompareHidden(); renderCompare(); }
function cmpShowPanel(alg) { _compareHidden.delete(alg); _saveCompareHidden(); renderCompare(); }

function renderCompare() {
    const grid = document.getElementById('compareGrid');
    if (!comparisonResults) {
        grid.innerHTML = '<div class="no-results">Klicke auf „Team Optimieren" um die Vergleichsansicht zu berechnen.</div>';
        return;
    }

    const allAlgs = Object.keys(comparisonResults);
    if (allAlgs.length === 0) { grid.innerHTML = '<div class="no-results">Keine Ergebnisse.</div>'; return; }

    const ordered   = _orderedAlgs(allAlgs);
    const algs       = ordered.filter(a => !_compareHidden.has(a));   // visible
    const hiddenAlgs = ordered.filter(a =>  _compareHidden.has(a));

    // Best value per metric (only among visible panels).
    const vis     = algs.map(a => comparisonResults[a]);
    const progRs  = vis.filter(r => r.isProg);
    const finalRs = vis.filter(r => r.isFinal);
    const bestPT     = Math.max(0, ...vis.map(r => r.totalPTCaptain || 0));
    const bestExp    = progRs.length  ? Math.max(...progRs.map(r => r.totalExpCaptain || 0)) : 0;
    const bestTP     = Math.max(0, ...vis.map(r => r.totalTP || 0));
    const bestCost   = Math.min(Infinity, ...vis.map(r => r.totalCost || Infinity));
    const bestSemi   = finalRs.length ? Math.max(...finalRs.map(r => r.semiFinalCount || 0)) : 0;
    const bestFinalC = finalRs.length ? Math.max(...finalRs.map(r => r.finalCount || 0)) : 0;

    // Union of all selected players (across visible panels), to mark shared rows.
    const allSelected = {};
    algs.forEach(a => comparisonResults[a].players.forEach(p => {
        allSelected[p.id] = (allSelected[p.id] || 0) + 1;
    }));

    const columns = algs.map(a => {
        const r    = comparisonResults[a];
        const meta = algLabel(a);
        const isProg = r.isProg, isFinal = r.isFinal, tree = r.tree;

        const playerRows = r.players
            .slice()
            .sort((x, y) => {
                if (isFinal) {
                    const vy = (y.treeRound?.[tree] ?? 0) * 1000 + y.avgPerTournament;
                    const vx = (x.treeRound?.[tree] ?? 0) * 1000 + x.avgPerTournament;
                    return vy - vx;
                }
                if (isProg) {
                    return (y.treeExpPoints?.[tree] ?? y.avgPerTournament)
                         - (x.treeExpPoints?.[tree] ?? x.avgPerTournament);
                }
                return y.avgPerTournament - x.avgPerTournament;
            })
            .map(p => {
                const inAll     = allSelected[p.id] === algs.length;
                const isCaptain = p.id === r.captainId;

                let ept = '';
                if (isFinal) {
                    const rv = p.treeRound?.[tree] ?? 0;
                    if (rv >= 2) {
                        ept = `<span class="cmp-mini cmp-round-badge cmp-round-final" title="Erreicht das Finale">F</span>`;
                    } else if (rv === 1) {
                        ept = `<span class="cmp-mini cmp-round-badge cmp-round-semi" title="Erreicht das Halbfinale">HF</span>`;
                    }
                } else if (isProg) {
                    const ev = p.treeExpPoints?.[tree];
                    if (ev != null) {
                        ept = `<span class="cmp-mini ${isCaptain ? 'cmp-captain-pts' : ''}"
                              title="${isCaptain ? 'Captain: 1,5× Punkte' : 'Erw. Punkte (' + (TREE_LABELS[tree] ?? tree) + ')'}">
                              ${(isCaptain ? ev * 1.5 : ev).toFixed(0)}</span>`;
                    }
                }

                const captainBadge = isCaptain
                    ? '<span class="cmp-captain-badge" title="Captain — 1,5× Punkte">C</span>'
                    : '';
                const estBadge = p.isEstimated
                    ? '<span class="cmp-estimated-badge" title="Statistiken geschätzt">~</span>'
                    : '';
                const lockBadge = lockedPlayerIds.has(p.id)
                    ? '<span class="cmp-lock-badge" title="Gesperrter Spieler">🔒</span>'
                    : '';
                return `
                <div class="cmp-player cmp-player-clickable ${inAll ? 'cmp-player-shared' : ''} ${isCaptain ? 'cmp-player-captain' : ''}"
                     onclick="openWhyChosen('${p.id}','${a}')"
                     title="Klicken: Warum wurde dieser Spieler gewählt?">
                    <div class="cmp-player-name">${captainBadge}${lockBadge}${p.name}${estBadge}</div>
                    <div class="cmp-player-meta">
                        <span class="cmp-pos cmp-pos-${p.pos.toLowerCase()}">${p.pos[0]}</span>
                        <span class="cmp-mini">${(isCaptain ? p.avgPerTournament * 1.5 : p.avgPerTournament).toFixed(0)} ⌀</span>
                        ${ept}
                        <span class="cmp-mini cmp-price">${p.price}₡</span>
                    </div>
                </div>`;
            }).join('');

        const expBestClass = (isProg && r.totalExpCaptain === bestExp && r.totalExpCaptain > 0) ? 'cmp-best' : '';

        const activeBanned = bannedPlayerIds.size;
        const lockedNote = (r.lockedCount > 0 || activeBanned > 0)
            ? `<div class="cmp-locked-note">${r.lockedCount > 0 ? `🔒 ${r.lockedCount} fest` : ''}${r.lockedCount > 0 && activeBanned > 0 ? ' · ' : ''}${activeBanned > 0 ? `🚫 ${activeBanned} ausgeschlossen` : ''}</div>`
            : '';
        const finalTotals = isFinal ? `
                <div class="cmp-total ${r.semiFinalCount === bestSemi && r.semiFinalCount > 0 ? 'cmp-best' : ''}">
                    <div class="cmp-total-label">HF+F-Spieler</div>
                    <div class="cmp-total-value">${r.semiFinalCount}</div>
                </div>
                <div class="cmp-total ${r.finalCount === bestFinalC && r.finalCount > 0 ? 'cmp-best' : ''}">
                    <div class="cmp-total-label">Finale-Spieler</div>
                    <div class="cmp-total-value">${r.finalCount}</div>
                </div>` : '';

        return `
        <div class="cmp-col ${isProg ? 'cmp-col-prog' : ''} ${isFinal ? 'cmp-col-final' : ''}"
             draggable="true" data-alg="${a}"
             ondragstart="cmpDragStart(event,'${a}')" ondragover="cmpDragOver(event)" ondrop="cmpDrop(event,'${a}')">
            <div class="cmp-head">
                <div class="cmp-head-top">
                    <span class="cmp-drag-handle" title="Ziehen zum Sortieren">⠿</span>
                    <div class="cmp-title">${meta.icon} ${meta.name}</div>
                    <button class="cmp-hide-btn" title="Panel ausblenden"
                            onclick="event.stopPropagation();cmpHidePanel('${a}')">✕</button>
                </div>
                <div class="cmp-desc">${meta.desc}</div>
                ${lockedNote}
            </div>
            <div class="cmp-totals">
                ${finalTotals}
                <div class="cmp-total ${expBestClass}" style="${isProg ? '' : 'display:none'}">
                    <div class="cmp-total-label">Erw. Punkte</div>
                    <div class="cmp-total-value">${(r.totalExpCaptain || 0).toFixed(0)}</div>
                </div>
                <div class="cmp-total ${r.totalPTCaptain === bestPT ? 'cmp-best' : ''}">
                    <div class="cmp-total-label">Σ Ø/Turnier</div>
                    <div class="cmp-total-value">${r.totalPTCaptain.toFixed(0)}</div>
                </div>
                <div class="cmp-total ${r.totalTP === bestTP ? 'cmp-best' : ''}">
                    <div class="cmp-total-label">Saison-Pts</div>
                    <div class="cmp-total-value">${r.totalTP.toFixed(0)}</div>
                </div>
                <div class="cmp-total ${r.totalCost === bestCost ? 'cmp-best' : ''}">
                    <div class="cmp-total-label">Kosten</div>
                    <div class="cmp-total-value">${r.totalCost}₡</div>
                </div>
            </div>
            <div class="cmp-positions">
                <span>B:${r.blockCount}</span>
                <span>A:${r.abwehrCount}</span>
                ${r.hybridCount > 0 ? `<span>H:${r.hybridCount}</span>` : ''}
            </div>
            <div class="cmp-players">${playerRows}</div>
        </div>`;
    }).join('');

    const hiddenBar = hiddenAlgs.length ? `
        <div class="cmp-hidden-bar">
            <span class="cmp-hidden-label">Ausgeblendet:</span>
            ${hiddenAlgs.map(a => `<button class="cmp-hidden-chip" onclick="cmpShowPanel('${a}')" title="Wieder einblenden">${algLabel(a).icon} ${algLabel(a).name} +</button>`).join('')}
        </div>` : '';

    updatePicksHint();
    grid.innerHTML = `${hiddenBar}<div class="cmp-grid">${columns}</div>
        <p class="tab-hint" style="margin-top:1.5rem">
            <span class="cmp-drag-handle" style="cursor:default">⠿</span> Panels ziehen zum Sortieren · ✕ blendet aus ·
            <span class="cmp-captain-badge" style="vertical-align:middle">C</span> = Captain (automatisch optimal gewählt, 1,5× Punkte) ·
            🔒 = Vorgesperrter Spieler (Picks-Tab) ·
            <strong>Σ Ø/Turnier &amp; Erw. Punkte</strong> enthalten bereits den Captain-Bonus ·
            Hervorgehobene Werte = Bester pro Metrik · Geteilte Spieler = unterstrichen ·
            <strong>~</strong> = Statistiken aus Poolschnitt geschätzt ·
            💡 <strong>Spielerkarte klicken</strong> = Begründung & Alternativen-Vergleich ·
            <span class="cmp-round-badge cmp-round-final" style="vertical-align:middle">F</span> = Finale ·
            <span class="cmp-round-badge cmp-round-semi" style="vertical-align:middle">HF</span> = Halbfinale (nur Finale-Fokus)
        </p>`;
}

// ── "Why was this player chosen?" modal ──────────────────────────────────────
//
// Triggered by clicking any player in the comparison-tab grid. Explains the
// pick by showing the algorithm's objective value for that player and listing
// nearby alternatives (same position, ±10₡) that weren't selected, sorted by
// the same objective metric. Each alternative has a 🆚 button that hands off
// to the H2H comparator with both players pre-filled.

// Metric metadata per algorithm — keyed by the same strings used in
// getObjectiveValue() / ALG_LABELS.
const OBJECTIVE_META = {
    'optimal':           { label: 'Ø Punkte/Turnier',           short: 'Ø/T',     digits: 1 },
    'consistent':        { label: 'Bayes-gedämpfter Ø',         short: 'adj Ø',   digits: 1 },
    'tournament':        { label: 'Erw. Punkte (Sim)',          short: 'Erw.',    digits: 0 },
    'tournament-manual': { label: 'Erw. Punkte (Manuell)',      short: 'Erw. M.', digits: 0 },
    'final-focus':       { label: 'Final-Round Score',          short: 'F-Score', digits: 0 },
};

// Full tournament-history table for the why-modal / H2H view.
// Shows ALL entries newest-first, with a year-divider row between years.
// Pass `wrap=false` to omit the outer .md-section block (for H2H side-by-side).
function renderHistoryTable(playerId, wrap = true) {
    const hist = (window.playerHistory || {})[playerId];
    if (!hist || hist.length === 0) return '';
    const ordered = hist.slice().reverse();   // newest first
    let lastYear = null;
    const rows = ordered.map(e => {
        const year = (e.dateEnd || '').slice(0, 4) || '?';
        const fp = e.fantasyPoints != null ? Number(e.fantasyPoints).toFixed(1) : '–';
        let header = '';
        if (year !== lastYear) {
            header = `<tr class="why-history-year"><td colspan="4">${escapeHtml(year)}</td></tr>`;
            lastYear = year;
        }
        return `${header}<tr>
            <td>${escapeHtml(e.tournamentName || '?')}</td>
            <td class="why-row-val">${e.dateEnd || ''}</td>
            <td class="why-row-val"><strong>${fp}</strong></td>
            <td class="why-row-val">${e.matches ?? '–'}</td>
        </tr>`;
    }).join('');
    const table = `
        <table class="why-stats why-history">
            <thead>
                <tr><th>Turnier</th><th>Datum</th><th>FP</th><th>Matches</th></tr>
            </thead>
            <tbody>${rows}</tbody>
        </table>`;
    if (!wrap) return table;
    return `
        <div class="md-section">
            <div class="md-section-title">📅 Turnier-Historie (${hist.length})</div>
            ${table}
        </div>`;
}

// Algorithm-agnostic player detail modal. Used from the "Alle Spieler" tab where
// no algorithm/team context exists. Shows the same stats + history as the
// why-modal but without the "why was this player chosen" framing.
function openPlayerDetail(playerId) {
    const player = (availablePlayers.find(p => p.id === playerId)
                   || allPlayers.find(p => p.id === playerId));
    if (!player) return;

    const fmt = (v, d = 1) => (v == null || isNaN(v)) ? '–' : Number(v).toFixed(d);
    const statRow = (label, val) => `
        <tr><td class="why-row-label">${label}</td><td class="why-row-val">${val}</td></tr>`;
    const isAvail = player.price > 0;
    const statsHtml = `
        <table class="why-stats">
            <tbody>
                ${statRow('Position', `<span class="cmp-pos cmp-pos-${player.pos.toLowerCase()}">${player.pos}</span>`)}
                ${statRow('Geschlecht', player.gender === 'M' ? '♂' : '♀')}
                ${statRow('Preis', isAvail ? `${player.price} ₡` : '<span style="color:var(--text-dim)">nicht verfügbar</span>')}
                ${statRow('Saison-Punkte', fmt(player.tp, 0))}
                ${statRow('Turniere / Matches', `${player.t} / ${player.mp}`)}
                ${statRow('Ø Punkte/Turnier', fmt(player.avgPerTournament, 1))}
                ${statRow('Ø Punkte/Match',   fmt(player.avgPerMatch, 2))}
                ${player.expectedMatches != null ? statRow('Erw. Matches (Sim)', fmt(player.expectedMatches, 2)) : ''}
                ${player.expectedPoints  != null ? statRow('Erw. Punkte (Sim)',  fmt(player.expectedPoints, 0)) : ''}
                ${player.varianceScore   != null ? statRow('Konsistenz-Score',  `${fmt(player.varianceScore, 1)} <span class="why-row-hint">(Ø − ½·Streuung)</span>`) : ''}
                ${player.formScore       != null ? statRow('Form (letzte 3)',   `${fmt(player.formScore, 1)} <span class="why-row-hint">(0,5·neu + 0,3·… + 0,2·…)</span>`) : ''}
                ${player.receptionRate   != null ? statRow('Annahme-Quote',     `${(player.receptionRate * 100).toFixed(0)} % <span class="why-row-hint">(good ÷ alle Annahmen)</span>`) : ''}
                ${player.attackEfficiency != null ? statRow('Angriffs-Effizienz', `${(player.attackEfficiency * 100).toFixed(0)} % <span class="why-row-hint">(Kills − Fehler − geblockt) ÷ Angriffe</span>`) : ''}
            </tbody>
        </table>`;
    const historyHtml = renderHistoryTable(player.id);

    closePlayerDetail();
    const modal = document.createElement('div');
    modal.id = 'playerDetailModal';
    modal.className = 'amb-modal';
    modal.onclick = (e) => { if (e.target === modal) closePlayerDetail(); };
    modal.innerHTML = `
        <div class="amb-dialog" style="max-width:720px">
            <div class="amb-header">
                <div>
                    <h2 style="margin:0">${escapeHtml(player.name)}</h2>
                    <p style="margin:0.3rem 0 0;font-size:0.85rem;color:var(--text-dim)">
                        Spieler-Details · Statistiken &amp; Turnier-Historie
                    </p>
                </div>
                <button class="amb-close" onclick="closePlayerDetail()" title="Schließen">×</button>
            </div>
            <div class="amb-body">
                <div class="md-section">
                    <div class="md-section-title">Eckdaten</div>
                    ${statsHtml}
                </div>
                ${historyHtml || ''}
            </div>
        </div>`;
    document.body.appendChild(modal);
}

function closePlayerDetail() {
    const m = document.getElementById('playerDetailModal');
    if (m) m.remove();
}

function openWhyChosen(playerId, alg) {
    const player = availablePlayers.find(p => p.id === playerId);
    const result = comparisonResults?.[alg];
    if (!player || !result) return;

    const algMeta = algLabel(alg);
    const objMeta = objectiveMeta(alg);
    const objVal  = getObjectiveValue(player, alg);
    const isCap   = result.captainId === player.id;
    const isLockd = lockedPlayerIds.has(player.id);

    // Build alternatives: same position, ±10₡, not in the team, has price > 0.
    const teamIds = new Set(result.players.map(x => x.id));
    const PRICE_WINDOW = 10;
    const alternatives = availablePlayers
        .filter(p =>
            !teamIds.has(p.id) &&
            p.pos === player.pos &&
            p.price > 0 &&
            Math.abs(p.price - player.price) <= PRICE_WINDOW
        )
        .map(p => ({ p, v: getObjectiveValue(p, alg) }))
        .sort((a, b) => b.v - a.v)
        .slice(0, 12);

    // Stat rows worth showing. We always show the objective metric first.
    const statRow = (label, val) => `
        <tr><td class="why-row-label">${label}</td><td class="why-row-val">${val}</td></tr>`;
    const fmt = (v, d = 1) => (v == null || isNaN(v)) ? '–' : Number(v).toFixed(d);
    const statsHtml = `
        <table class="why-stats">
            <tbody>
                ${statRow(`<strong>${objMeta.label}</strong> <span class="why-objective-tag">Algorithmus-Ziel</span>`,
                          `<strong class="why-objective-val">${fmt(objVal, objMeta.digits)}</strong>`)}
                ${statRow('Position', `<span class="cmp-pos cmp-pos-${player.pos.toLowerCase()}">${player.pos}</span>`)}
                ${statRow('Preis', `${player.price} ₡`)}
                ${statRow('Saison-Punkte', fmt(player.tp, 0))}
                ${statRow('Turniere / Matches', `${player.t} / ${player.mp}`)}
                ${statRow('Ø Punkte/Turnier', fmt(player.avgPerTournament, 1))}
                ${statRow('Ø Punkte/Match',   fmt(player.avgPerMatch, 2))}
                ${player.expectedMatches != null ? statRow('Erw. Matches (Sim)', fmt(player.expectedMatches, 2)) : ''}
                ${player.expectedPoints  != null ? statRow('Erw. Punkte (Sim)',  fmt(player.expectedPoints, 0)) : ''}
                ${statRow('Pts / Coin (Effizienz)', fmt(objVal / player.price, 2))}
                ${player.receptionRate    != null ? statRow('Annahme-Quote', `${(player.receptionRate * 100).toFixed(0)} % <span class="why-row-hint">(good ÷ alle Annahmen)</span>`) : ''}
                ${player.attackEfficiency != null ? statRow('Angriffs-Effizienz', `${(player.attackEfficiency * 100).toFixed(0)} % <span class="why-row-hint">(Kills − Fehler − geblockt) ÷ alle Angriffe</span>`) : ''}
                ${player.varianceScore    != null ? statRow('Konsistenz-Score', `${fmt(player.varianceScore, 1)} <span class="why-row-hint">(Ø − ½·Streuung der letzten Turniere)</span>`) : ''}
                ${player.formScore        != null ? statRow('Form (letzte 3)', `${fmt(player.formScore, 1)} <span class="why-row-hint">(0,5·neu + 0,3·… + 0,2·…)</span>`) : ''}
            </tbody>
        </table>`;
    const historyHtml = renderHistoryTable(player.id);

    const reasonBadges = [];
    if (isCap)    reasonBadges.push('<span class="cmp-captain-badge">C</span> Captain (1,5×)');
    if (isLockd)  reasonBadges.push('🔒 Vom Nutzer vorgesperrt');
    const reasonHtml = reasonBadges.length
        ? `<div class="why-reason-badges">${reasonBadges.join(' · ')}</div>` : '';

    // Plain-language explanation tailored to the algorithm
    const explanation = renderWhyExplanation(player, alg, alternatives);

    // Alternatives table — each row clickable for an H2H comparison.
    const altRows = alternatives.length === 0
        ? `<tr><td colspan="4" class="why-no-alts">Keine vergleichbaren Alternativen
            (gleiche Position, ±${PRICE_WINDOW}₡) im verfügbaren Pool.</td></tr>`
        : alternatives.map(({ p, v }) => {
            const delta = v - objVal;
            const cls   = delta > 0 ? 'why-alt-better' : delta < 0 ? 'why-alt-worse' : '';
            const sign  = delta > 0 ? '+' : '';
            const reason = whyNotChosenReason(p, player, v, objVal);
            return `
            <tr class="${cls}">
                <td>
                    <div class="why-alt-name">${escapeHtml(p.name)}</div>
                    <div class="why-alt-reason">${reason}</div>
                </td>
                <td class="why-row-val">${p.price}₡</td>
                <td class="why-row-val">
                    <strong>${fmt(v, objMeta.digits)}</strong>
                    ${delta !== 0 ? `<span class="why-delta">(${sign}${fmt(delta, objMeta.digits)})</span>` : ''}
                </td>
                <td class="why-row-val">
                    <button class="btn-inline" style="font-size:0.78rem;padding:0.3rem 0.6rem"
                            onclick="openH2HCompareWith('${player.id}','${p.id}')">🆚 H2H</button>
                </td>
            </tr>`;
        }).join('');

    closeWhyChosen();
    const modal = document.createElement('div');
    modal.id = 'whyModal';
    modal.className = 'amb-modal';
    modal.onclick = (e) => { if (e.target === modal) closeWhyChosen(); };
    modal.innerHTML = `
        <div class="amb-dialog" style="max-width:860px">
            <div class="amb-header">
                <div>
                    <h2 style="margin:0">${algMeta.icon} Warum wurde <span class="why-player-name">${escapeHtml(player.name)}</span> gewählt?</h2>
                    <p style="margin:0.3rem 0 0;font-size:0.85rem;color:var(--text-dim)">
                        Algorithmus: <strong>${escapeHtml(algMeta.name)}</strong> — ${escapeHtml(algMeta.desc)}
                    </p>
                </div>
                <button class="amb-close" onclick="closeWhyChosen()" title="Schließen">×</button>
            </div>
            <div class="amb-body">
                ${reasonHtml}
                <div class="md-section">
                    <div class="md-section-title">Spieler-Eckdaten</div>
                    ${statsHtml}
                </div>
                ${historyHtml}
                <div class="md-section">
                    <div class="md-section-title">Begründung</div>
                    <div class="why-explanation">${explanation}</div>
                </div>
                <div class="md-section">
                    <div class="md-section-title">
                        Vergleichbare Alternativen (gleiche Position, ±${PRICE_WINDOW}₡)
                    </div>
                    <table class="why-alts">
                        <thead>
                            <tr>
                                <th>Spieler</th>
                                <th>Preis</th>
                                <th>${objMeta.short}</th>
                                <th></th>
                            </tr>
                        </thead>
                        <tbody>${altRows}</tbody>
                    </table>
                    <p class="tab-hint" style="margin-top:0.6rem">
                        🆚-Button öffnet den vollen Spieler-Vergleich. „+/−"-Zahl = Differenz im Algorithmus-Wert.
                    </p>
                </div>
            </div>
        </div>`;
    document.body.appendChild(modal);
}

function closeWhyChosen() {
    const m = document.getElementById('whyModal');
    if (m) m.remove();
}

// One-line explanation tailored to the algorithm's logic.
function renderWhyExplanation(player, alg, alternatives) {
    const objMeta = objectiveMeta(alg);
    const v       = getObjectiveValue(player, alg);
    const better  = alternatives.filter(a => a.v > v);
    const worse   = alternatives.filter(a => a.v < v);
    const pricePerf = v / player.price;
    const fmt = (x, d = 1) => Number(x).toFixed(d);

    const intros = {
        'optimal':    `Der <strong>Optimal</strong>-Algorithmus maximiert die Summe von <em>Ø Punkten/Turnier</em>
                       über die ganze Saison — er belohnt rohe Konstanz.`,
        'consistent': `<strong>Konsistent</strong> verwendet einen Bayes-gedämpften Ø-Wert
                       (kleinere Stichproben werden zum Pool-Mittel hingezogen, k=3),
                       damit Spieler mit wenigen Turnieren nicht überschätzt werden.`,
        'form-trend': `<strong>Form-Trend</strong> gewichtet die letzten Turniere stärker als frühere.`,
    };
    let intro = intros[alg];
    if (!intro && alg.startsWith('prog-')) {
        const t = TREE_LABELS[alg.slice(5)] ?? alg.slice(5);
        intro = `<strong>Turnier-Prognose (${t})</strong> multipliziert die erwarteten Matches aus dem
                 <em>${t}</em>-Turnierbaum mit dem persönlichen Punkteschnitt pro Match.`;
    } else if (!intro && alg.startsWith('final-')) {
        const t = TREE_LABELS[alg.slice(6)] ?? alg.slice(6);
        intro = `<strong>Finale-Fokus (${t})</strong> priorisiert Spieler, deren Teams laut
                 <em>${t}</em>-Baum das Halbfinale (HF) oder Finale (F) erreichen — Konstanz tritt in den Hintergrund.`;
    }
    if (!intro) intro = 'Dieser Algorithmus maximiert eine spezifische Zielmetrik.';

    let comparison = '';
    if (alternatives.length === 0) {
        comparison = `Es gibt keine Spieler gleicher Position im Preisfenster (±10₡), gegen die dieser Pick verglichen werden könnte.`;
    } else if (better.length === 0) {
        comparison = `Dieser Spieler hat den <strong>höchsten ${objMeta.label}</strong>
                      unter allen vergleichbaren Kandidaten (gleiche Position, ±10₡). Hier wurde "der Beste seiner Preisklasse" eingepackt.`;
    } else {
        const top = better[0];
        comparison = `Es gibt ${better.length} ${better.length === 1 ? 'Alternative' : 'Alternativen'} mit höherem
                      <em>${objMeta.label}</em> (${escapeHtml(top.p.name)} liegt bei ${fmt(top.v, objMeta.digits)} vs. ${fmt(v, objMeta.digits)} hier) —
                      diese passten aber nicht ins Team, weil <strong>Budget, Slot-Limits</strong> oder
                      <strong>Position-Caps (Block/Abwehr)</strong> sie ausgeschlossen haben.
                      Der Optimizer hat die globale Summe maximiert, nicht jeden Einzel-Slot.`;
    }

    const efficiencyNote = `Effizienz <strong>${fmt(pricePerf, 2)} ${objMeta.short}/Coin</strong> ·
                            ${worse.length}/${alternatives.length} Alternativen liegen darunter.`;

    return `<p style="margin:0 0 0.6rem">${intro}</p>
            <p style="margin:0 0 0.6rem">${comparison}</p>
            <p style="margin:0;color:var(--text-dim);font-size:0.85rem">${efficiencyNote}</p>`;
}

function whyNotChosenReason(alt, picked, altVal, pickedVal) {
    if (altVal > pickedVal) {
        // Better metric but not picked — must be a constraint (budget / slot / position cap)
        if (alt.price > picked.price) {
            return `Höherer ${OBJECTIVE_META.optimal.short || 'Wert'}, aber teurer (${alt.price - picked.price}₡ über dem gewählten Pick).
                    Wahrscheinlich aus Budget-/Slot-Gründen ausgeschlossen.`;
        }
        return `Höherer Wert bei gleichem/günstigerem Preis — wahrscheinlich durch eine
                Block-/Abwehr-Obergrenze oder Locked-Player-Belegung verdrängt.`;
    }
    if (altVal === pickedVal) {
        return `Gleicher Algorithmus-Wert — Tiebreaker entschied zugunsten von ${escapeHtml(picked.name)}.`;
    }
    return `Niedrigerer Algorithmus-Wert (Δ ${(altVal - pickedVal).toFixed(2)}).`;
}

// ── H2H quick-compare modal ───────────────────────────────────────────────────
//
// Lets the user pick any two players and see their stats side-by-side, plus any
// individual H2H data the current bracket prediction has between them.
// Data sources, all client-side:
//   • allPlayers           — stats (tp, t, mp, avg/T, avg/Match, price, pos…)
//   • availablePlayers     — adds expectedPoints, expectedMatches (sim)
//   • tournamentSim.byGender[g].bracketPrediction[*].indBreakdown
//                          — individual H2H records by last-name pair (only
//                            available when both players' teams meet in the
//                            current bracket prediction)

const h2hCompare = { a: null, b: null };

function openH2HCompare() {
    closeH2HCompare();
    const modal = document.createElement('div');
    modal.id = 'h2hModal';
    modal.className = 'amb-modal';
    modal.onclick = (e) => { if (e.target === modal) closeH2HCompare(); };
    modal.innerHTML = `
        <div class="amb-dialog" style="max-width:880px">
            <div class="amb-header">
                <div>
                    <h2 style="margin:0">🆚 H2H Spieler-Vergleich</h2>
                    <p style="margin:0.3rem 0 0;font-size:0.85rem;color:var(--text-dim)">
                        Wähle zwei Spieler, um Statistiken und Einzel-Bilanz nebeneinander zu sehen.
                    </p>
                </div>
                <button class="amb-close" onclick="closeH2HCompare()" title="Schließen">×</button>
            </div>
            <div class="amb-body" id="h2hBody">${renderH2HBody()}</div>
        </div>`;
    document.body.appendChild(modal);
}

function closeH2HCompare() {
    const m = document.getElementById('h2hModal');
    if (m) m.remove();
}

// One shared datalist of every player — used by both H2H slots' search boxes.
// Browsers do their own substring/prefix matching on `<option value>` so we
// just need to emit one option per player.
function h2hDatalistOptions() {
    return allPlayers
        .slice()
        .sort((a, b) => a.name.localeCompare(b.name, 'de'))
        .map(p => {
            const tag = p.gender === 'M' ? '♂' : '♀';
            const meta = p.price > 0 ? ` · ₡${p.price}` : '';
            const pos  = p.pos ? ` · ${p.pos}` : '';
            // Browsers show the `label` next to the value in the dropdown
            return `<option value="${escapeAttr(p.name)}" label="${escapeAttr(`${tag}${pos}${meta}`)}"></option>`;
        }).join('');
}

// Called on every keystroke in a search box. We only update the RESULTS
// section below (#h2hResults) — never the input itself — so the user's
// caret and typed text are never disturbed.
function onH2HSearchInput(slot, value) {
    const v = (value || '').trim().toLowerCase();
    const prev = h2hCompare[slot];
    let next = null;
    if (v) {
        const exact = allPlayers.find(p => p.name.toLowerCase() === v);
        if (exact) {
            next = exact.id;
        } else {
            // Auto-accept when there's exactly one substring match — useful
            // when the user types "Tamo" and only one player matches.
            const subs = allPlayers.filter(p => p.name.toLowerCase().includes(v));
            if (subs.length === 1) next = subs[0].id;
        }
    }
    if (next !== prev) {
        h2hCompare[slot] = next;
        const r = document.getElementById('h2hResults');
        if (r) r.innerHTML = renderH2HResults();
    }
}

function renderH2HBody() {
    const a = h2hCompare.a ? allPlayers.find(p => p.id === h2hCompare.a) : null;
    const b = h2hCompare.b ? allPlayers.find(p => p.id === h2hCompare.b) : null;
    const valA = a?.name ?? '';
    const valB = b?.name ?? '';

    // Pickers are rendered exactly ONCE per modal open. Subsequent typing only
    // re-renders #h2hResults below, so the input keeps focus + caret position.
    const pickers = `
        <div class="h2h-pickers">
            <div class="h2h-picker">
                <label class="filter-label" for="h2hSearchA">Spieler A — tippen zum Suchen</label>
                <input id="h2hSearchA" class="h2h-search" list="h2hPlayerList" data-slot="a"
                       value="${escapeAttr(valA)}" autocomplete="off"
                       placeholder="Name eingeben…"
                       oninput="onH2HSearchInput('a', this.value)">
            </div>
            <div class="h2h-vs">VS</div>
            <div class="h2h-picker">
                <label class="filter-label" for="h2hSearchB">Spieler B — tippen zum Suchen</label>
                <input id="h2hSearchB" class="h2h-search" list="h2hPlayerList" data-slot="b"
                       value="${escapeAttr(valB)}" autocomplete="off"
                       placeholder="Name eingeben…"
                       oninput="onH2HSearchInput('b', this.value)">
            </div>
            <datalist id="h2hPlayerList">${h2hDatalistOptions()}</datalist>
        </div>
        <div id="h2hResults">${renderH2HResults()}</div>`;
    return pickers;
}

function renderH2HResults() {
    const a = h2hCompare.a ? allPlayers.find(p => p.id === h2hCompare.a) : null;
    const b = h2hCompare.b ? allPlayers.find(p => p.id === h2hCompare.b) : null;
    if (!a || !b) {
        return `<p style="color:var(--text-dim);text-align:center;margin:1.5rem 0">
            Tippe in beide Felder einen Namen (Autovervollständigung verfügbar).</p>`;
    }
    if (a.id === b.id) {
        return `<p style="color:var(--warning);text-align:center;margin:1.5rem 0">
            Bitte zwei verschiedene Spieler wählen.</p>`;
    }
    return renderH2HStatsCompare(a, b)
         + renderH2HHistoryCompare(a, b)
         + renderH2HIndividualSection(a, b);
}

// Side-by-side per-tournament fantasyPoints history. Two compact tables —
// one per player. Returns empty string if neither player has history.
function renderH2HHistoryCompare(a, b) {
    const tableA = renderHistoryTable(a.id, false);
    const tableB = renderHistoryTable(b.id, false);
    if (!tableA && !tableB) return '';
    const cell = (name, table) => `
        <div>
            <div class="h2h-history-name">${escapeHtml(name)}</div>
            ${table || '<p class="h2h-no-history">Keine Turnier-Historie verfügbar.</p>'}
        </div>`;
    return `
        <div class="md-section">
            <div class="md-section-title">📅 Turnier-Historie</div>
            <div class="h2h-history-grid">
                ${cell(a.name, tableA)}
                ${cell(b.name, tableB)}
            </div>
        </div>`;
}

// Public helper used by other modals to jump straight into the comparator
// with both slots pre-filled.
function openH2HCompareWith(idA, idB) {
    h2hCompare.a = idA || null;
    h2hCompare.b = idB || null;
    openH2HCompare();
}

function renderH2HStatsCompare(a, b) {
    const avA = availablePlayers.find(p => p.id === a.id);
    const avB = availablePlayers.find(p => p.id === b.id);

    // Pull expected* from the available copy if the player is in the sim
    const ePtsA = avA?.expectedPoints, ePtsB = avB?.expectedPoints;
    const eMatA = avA?.expectedMatches, eMatB = avB?.expectedMatches;
    const fmt   = (v, dp = 0) => (v === null || v === undefined) ? '–' : v.toFixed(dp);
    const better = (va, vb, higherIsBetter = true) => {
        if (va == null || vb == null || va === vb) return ['', ''];
        const aBetter = higherIsBetter ? va > vb : va < vb;
        return aBetter ? ['h2h-better', ''] : ['', 'h2h-better'];
    };

    const rows = [
        { label: 'Position',          va: a.pos,                 vb: b.pos,                 cmp: ['', ''] },
        { label: 'Geschlecht',        va: a.gender === 'M' ? '♂' : '♀', vb: b.gender === 'M' ? '♂' : '♀', cmp: ['', ''] },
        { label: 'Preis (₡)',         va: a.price > 0 ? a.price : '–', vb: b.price > 0 ? b.price : '–',
          cmp: better(a.price > 0 ? a.price : null, b.price > 0 ? b.price : null, false) },
        { label: 'Saison-Punkte',     va: fmt(a.tp),             vb: fmt(b.tp),             cmp: better(a.tp, b.tp) },
        { label: 'Turniere',          va: a.t,                   vb: b.t,                   cmp: better(a.t, b.t) },
        { label: 'Matches',           va: a.mp,                  vb: b.mp,                  cmp: better(a.mp, b.mp) },
        { label: 'Ø Punkte/Turnier',  va: fmt(a.avgPerTournament, 1), vb: fmt(b.avgPerTournament, 1),
          cmp: better(a.avgPerTournament, b.avgPerTournament) },
        { label: 'Ø Punkte/Match',    va: fmt(a.avgPerMatch, 2), vb: fmt(b.avgPerMatch, 2), cmp: better(a.avgPerMatch, b.avgPerMatch) },
        { label: 'Pts/Coin',          va: fmt(a.avgPerCoin, 2),  vb: fmt(b.avgPerCoin, 2),  cmp: better(a.avgPerCoin, b.avgPerCoin) },
        { label: 'Erw. Matches (Sim)', va: fmt(eMatA, 2),        vb: fmt(eMatB, 2),         cmp: better(eMatA, eMatB) },
        { label: 'Erw. Punkte (Sim)', va: fmt(ePtsA, 0),         vb: fmt(ePtsB, 0),         cmp: better(ePtsA, ePtsB) },
        { label: 'Annahme-Quote',     va: avA?.receptionRate    != null ? `${(avA.receptionRate*100).toFixed(0)} %` : '–',
                                       vb: avB?.receptionRate    != null ? `${(avB.receptionRate*100).toFixed(0)} %` : '–',
                                       cmp: better(avA?.receptionRate, avB?.receptionRate) },
        { label: 'Angriffs-Effizienz', va: avA?.attackEfficiency != null ? `${(avA.attackEfficiency*100).toFixed(0)} %` : '–',
                                       vb: avB?.attackEfficiency != null ? `${(avB.attackEfficiency*100).toFixed(0)} %` : '–',
                                       cmp: better(avA?.attackEfficiency, avB?.attackEfficiency) },
        { label: 'Form (letzte 3)',    va: fmt(avA?.formScore, 1), vb: fmt(avB?.formScore, 1),
                                       cmp: better(avA?.formScore, avB?.formScore) },
    ];

    const rowsHtml = rows.map(r => `
        <tr>
            <td class="h2h-row-label">${r.label}</td>
            <td class="h2h-row-val ${r.cmp[0]}">${r.va}</td>
            <td class="h2h-row-val ${r.cmp[1]}">${r.vb}</td>
        </tr>`).join('');

    return `
    <div class="md-section h2h-compare-section">
        <table class="h2h-table">
            <thead>
                <tr>
                    <th></th>
                    <th class="h2h-name">${escapeHtml(a.name)}</th>
                    <th class="h2h-name">${escapeHtml(b.name)}</th>
                </tr>
            </thead>
            <tbody>${rowsHtml}</tbody>
        </table>
    </div>`;
}

// Look through every bracketPrediction match for an individual H2H entry that
// involves the two players' last names (the only place client-side where
// player-vs-player records live).
function renderH2HIndividualSection(a, b) {
    if (!tournamentSim) {
        return `<p style="color:var(--text-dim);text-align:center;margin:0.5rem 0 0">
            Keine Sim-Daten geladen — Einzel-H2H nicht verfügbar.</p>`;
    }
    const lastA = (a.lastName || '').toLowerCase();
    const lastB = (b.lastName || '').toLowerCase();
    const indEntries = [];
    const byG = tournamentSim.byGender || {};
    for (const g of Object.keys(byG)) {
        const matches = byG[g]?.bracketPrediction || [];
        for (const m of matches) {
            if (!m.indBreakdown?.length) continue;
            for (const r of m.indBreakdown) {
                const pA = (r.playerA || '').toLowerCase();
                const pB = (r.playerB || '').toLowerCase();
                const hitForward  = pA.includes(lastA) && pB.includes(lastB);
                const hitBackward = pA.includes(lastB) && pB.includes(lastA);
                if (hitForward) {
                    indEntries.push({ side: 'fwd', wA: r.wA, wB: r.wB, playerA: r.playerA, playerB: r.playerB,
                                       context: `${m.teamA} vs ${m.teamB}` });
                } else if (hitBackward) {
                    indEntries.push({ side: 'bwd', wA: r.wB, wB: r.wA, playerA: r.playerB, playerB: r.playerA,
                                       context: `${m.teamA} vs ${m.teamB}` });
                }
            }
        }
    }
    if (!indEntries.length) {
        return `<div class="md-section">
            <div class="md-section-title">Einzel-H2H Bilanz</div>
            <p style="color:var(--text-dim);margin:0.5rem 0 0">
                Keine direkten Einzel-Bilanzen in der aktuellen Bracket-Prognose.
                <br><small>(Die Spieler treffen im vorhergesagten Verlauf nicht aufeinander.)</small>
            </p>
        </div>`;
    }
    // Deduplicate (same pairing may appear in multiple matches)
    const seen = new Set();
    const dedup = [];
    for (const e of indEntries) {
        const k = `${e.playerA}|${e.playerB}|${e.wA}-${e.wB}`;
        if (seen.has(k)) continue;
        seen.add(k);
        dedup.push(e);
    }
    const totalA = dedup.reduce((s, e) => s + e.wA, 0);
    const totalB = dedup.reduce((s, e) => s + e.wB, 0);
    const total  = totalA + totalB;
    const pct    = total > 0 ? (totalA / total * 100).toFixed(0) : '–';

    const rows = dedup.map(e => `
        <div class="md-h2h-row md-ind-row">
            <span class="${e.wA > e.wB ? 'md-ind-winner' : ''}">${escapeHtml(e.playerA)}</span>
            <strong class="md-score">${e.wA} : ${e.wB}</strong>
            <span class="${e.wB > e.wA ? 'md-ind-winner' : ''}">${escapeHtml(e.playerB)}</span>
            <span class="md-ind-pct">(${escapeHtml(e.context)})</span>
        </div>`).join('');

    return `
    <div class="md-section">
        <div class="md-section-title">Einzel-H2H Bilanz</div>
        ${rows}
        <div class="md-h2h-meta">
            Gesamt: <strong>${totalA}</strong> : <strong>${totalB}</strong>
            (${total} Spiele insgesamt${total > 0 ? `, ${pct}&nbsp;% für ${escapeHtml(a.name)}` : ''})
        </div>
    </div>`;
}

// ── Init ──────────────────────────────────────────────────────────────────────

function initPlayerFilters() {
    // Event delegation on document.body — works for both the static players-tab
    // filter bar and the picks-tab filter bar that's rendered dynamically.
    document.body.addEventListener('change', (e) => {
        const t = e.target;
        if (!t) return;
        if (t.matches('.filter-check input[type=checkbox]')) onFilterChange(e);
        else if (t.matches('select[data-sort-select]'))     onSortChange(e);
        // Price boxes use `change` (fires on blur / Enter) only — `input` would
        // re-render after every digit and yank the cursor out mid-typing.
        else if (t.matches('input.filter-price-input'))     onPriceFilterChange(e);
    });
    // Pressing Enter inside a price input shouldn't submit anything; blur it
    // so `change` fires and the filter applies immediately.
    document.body.addEventListener('keydown', (e) => {
        if (e.key === 'Enter' && e.target?.matches?.('input.filter-price-input')) {
            e.preventDefault();
            e.target.blur();
        }
    });
}

function initBracketGenderToggle() {
    document.querySelectorAll('#bracketGenderToggle .filter-pill').forEach(b => {
        b.addEventListener('click', () => setBracketGender(b.dataset.bg));
    });
}

function initVersusGenderToggle() {
    document.querySelectorAll('#versusGenderToggle .filter-pill').forEach(b => {
        b.addEventListener('click', () => setVersusGender(b.dataset.bg));
    });
}

initPlayerFilters();
initBracketGenderToggle();
initVersusGenderToggle();

// ── App start: bypass login when Supabase isn't configured (Self-Host) ──
function _startApp(session) {
    const lo = document.getElementById('loginOverlay');
    if (lo) lo.hidden = true;
    window.USER_ROLE = _decodeRole(session);
    applyRoleVisibility(window.USER_ROLE);
    loadData();
}

// Hide tabs / controls that the current role isn't allowed to access. Tabs are
// tagged in index.html via `data-min-role`. Untagged tabs are visible to all.
// If the currently-active tab gets hidden, fall back to the ELO ranking tab
// (the one tab everyone can see).
function applyRoleVisibility(role) {
    const tabBtns = document.querySelectorAll('.tab[data-min-role]');
    const tabIdMap = { players: 'playersTab', picks: 'picksTab',
        compare: 'compareTab', bracket: 'bracketTab',
        elo: 'eloTab', elotune: 'elotuneTab' };
    let hidActive = false;
    tabBtns.forEach(btn => {
        const need = btn.getAttribute('data-min-role');
        const allowed = roleAtLeast(role, need);
        btn.hidden = !allowed;
        // Derive content-pane id from the onclick="switchTab('X', this)" attr.
        const m = (btn.getAttribute('onclick') || '').match(/switchTab\('([^']+)'/);
        const contentId = m && tabIdMap[m[1]];
        if (contentId) {
            const pane = document.getElementById(contentId);
            if (pane) {
                if (!allowed && pane.classList.contains('active')) hidActive = true;
                if (!allowed) pane.classList.remove('active');
            }
        }
    });
    // Buttons / controls outside the tab bar also key off data-min-role.
    document.querySelectorAll('[data-min-role]').forEach(el => {
        if (el.classList.contains('tab')) return;
        el.hidden = !roleAtLeast(role, el.getAttribute('data-min-role'));
    });
    // Cloud mode (SUPABASE_URL set) never computes ELO — ratings are built
    // locally and uploaded via scripts/elo/publish.py. Hide all compute UI
    // (Tuning tab, Aktualisieren button, stale banner) regardless of role.
    // Self-host (no SUPABASE_URL) leaves them visible.
    if (window.SUPABASE_URL) {
        document.querySelectorAll('[data-local-only]').forEach(el => {
            el.hidden = true;
            const m = (el.getAttribute('onclick') || '').match(/switchTab\('([^']+)'/);
            const contentId = m && tabIdMap[m[1]];
            if (contentId) {
                const pane = document.getElementById(contentId);
                if (pane) {
                    if (pane.classList.contains('active')) hidActive = true;
                    pane.classList.remove('active');
                }
            }
        });
    }
    if (hidActive) {
        const eloBtn = Array.from(document.querySelectorAll('.tab'))
            .find(b => (b.getAttribute('onclick') || '').includes("switchTab('elo'"));
        if (eloBtn && !eloBtn.hidden) switchTab('elo', eloBtn);
    }
    // Drop admin-only transient elements that might have rendered before the
    // role applied (sync warnings, sim banner).
    if (!roleAtLeast(role, 'admin')) {
        const sw = document.getElementById('syncWarnings'); if (sw) sw.remove();
        const sb = document.getElementById('simBanner');    if (sb) sb.remove();
    }
}

function _showLogin() {
    const lo = document.getElementById('loginOverlay');
    if (lo) lo.hidden = false;
    const btn = document.getElementById('logoutBtn');
    if (btn) btn.hidden = true;
}

if (!supa) {
    // No Supabase configured → start immediately, never show login.
    _startApp();
} else {
    // Cloud mode: gate the app behind a Supabase session.
    const btn = document.getElementById('logoutBtn');
    if (btn) btn.hidden = false;

    const form = document.getElementById('loginForm');
    if (form) {
        form.addEventListener('submit', async (e) => {
            e.preventDefault();
            const email = document.getElementById('loginEmail').value.trim();
            const password = document.getElementById('loginPassword').value;
            const errEl = document.getElementById('loginError');
            errEl.hidden = true;
            const { error } = await supa.auth.signInWithPassword({ email, password });
            if (error) {
                errEl.textContent = 'Login fehlgeschlagen: ' + error.message;
                errEl.hidden = false;
            }
            // Successful login fires onAuthStateChange below.
        });
    }

    let _appStarted = false;
    supa.auth.onAuthStateChange((_event, session) => {
        if (session) {
            if (!_appStarted) { _appStarted = true; _startApp(session); }
        } else {
            _appStarted = false;
            _showLogin();
        }
    });

    supa.auth.getSession().then(({ data }) => {
        if (data?.session) {
            _appStarted = true;
            _startApp(data.session);
        } else {
            _showLogin();
        }
    });
}
