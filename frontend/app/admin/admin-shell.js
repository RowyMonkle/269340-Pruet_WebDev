/* ENCORE Backstage shell, loaded first on every admin page (after ../session.js).
 *
 * - Gate: role "admin" sees every page (seed: user id 1); role "organizer" sees only
 *   Concerts & seating (seed: user ids 2-15); fans get a log-in gate.
 *   This is a front-door check only; the read APIs used here are public anyway, and the one
 *   protected admin API (/admin/flags) still needs the X-Admin-Token header (see settings.html).
 * - Draws the sidebar + toolbar into <body data-page="..."> and exposes window.Admin helpers.
 */
(() => {
  "use strict";

  const API_BASE = "/api/v1";
  const CURRENCY = "฿";
  const PARALLEL = 8;                         // detail requests at the same time
  const CACHE_KEY = "encore.admin.events";    // sessionStorage cache of event details
  const CACHE_MS = 2 * 60 * 1000;             // reuse for 2 minutes when switching admin pages
  const TOKEN_KEY = "encore.admin.token";     // X-Admin-Token, this tab only (sessionStorage)

  const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
  const money = (n, dp = 0) => `${CURRENCY}${Number(n || 0).toLocaleString("en-US", { minimumFractionDigits: dp, maximumFractionDigits: dp })}`;
  const num = (n) => Number(n || 0).toLocaleString("en-US");
  const asDate = (s) => (s ? new Date(/[zZ]|[+-]\d\d:\d\d$/.test(s) ? s : s + "Z") : null);
  const fmtDate = (d, time = false) => (d && !Number.isNaN(d.getTime())
    ? d.toLocaleDateString("en-GB", { day: "numeric", month: "short", year: "numeric" }) + (time ? ` · ${d.toLocaleTimeString("en-GB", { hour: "2-digit", minute: "2-digit" })}` : "")
    : "—");

  // ---------- gate ----------
  const me = window.EncoreSession?.get();
  const page = document.body.dataset.page;
  // which Backstage pages each role may open (organizers: concerts only, no user lists or settings)
  const ACCESS = { admin: ["overview", "concerts", "users", "settings"], organizer: ["concerts"] };
  const allowed = ACCESS[me?.role] || [];
  if (allowed.length && !allowed.includes(page)) {   // e.g. an organizer opening /admin/ or users.html
    location.replace("concerts.html");
    window.Admin = null;
    return;
  }
  if (!allowed.length) {
    document.body.innerHTML = `
      <main class="gate">
        <h1>Backstage is for admins and organizers</h1>
        <p>${me ? `You’re logged in as ${esc(me.email)}, which is a fan account.` : "Log in with an admin or organizer account to continue."}</p>
        <a class="btn btn--primary" href="${esc(window.EncoreSession.loginHref())}">${me ? "Log in with another account" : "Log in"}</a>
        <a class="panel__link" href="/">← Back to the website</a>
      </main>`;
    window.Admin = null;
    return;
  }

  // ---------- shell ----------
  const ICON = {   // simple line icons (Lucide-style), drawn with currentColor
    overview: '<path d="M3 3h7v9H3zM14 3h7v5h-7zM14 12h7v9h-7zM3 16h7v5H3z"/>',
    concerts: '<path d="M9 18V5l12-2v13"/><circle cx="6" cy="18" r="3"/><circle cx="18" cy="16" r="3"/>',
    payments: '<rect x="2" y="5" width="20" height="14" rx="2"/><path d="M2 10h20"/>',
    users: '<path d="M16 21v-2a4 4 0 0 0-4-4H6a4 4 0 0 0-4 4v2"/><circle cx="9" cy="7" r="4"/><path d="M22 21v-2a4 4 0 0 0-3-3.9M16 3.1a4 4 0 0 1 0 7.8"/>',
    settings: '<circle cx="12" cy="12" r="3"/><path d="M12 2v3M12 19v3M4.2 4.2l2.1 2.1M17.7 17.7l2.1 2.1M2 12h3M19 12h3M4.2 19.8l2.1-2.1M17.7 6.3l2.1-2.1"/>',
  };
  const svg = (k) => `<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">${ICON[k]}</svg>`;
  const NAV = [
    ["overview", "Overview", "index.html"],
    ["concerts", "Concerts & seating", "concerts.html"],
    ["users", "Users", "users.html"],
    ["settings", "Workspace settings", "settings.html"],
  ].filter(([k]) => allowed.includes(k));   // organizers only see Concerts & seating
  const isAdmin = me.role === "admin";
  const TITLES = { overview: "Overview", concerts: "Concerts & seating", users: "Users", settings: "Workspace settings" };
  const initials = (me.first_name || me.email || "?").slice(0, 2).toUpperCase();

  const content = document.getElementById("content");
  const shell = document.createElement("div");
  shell.className = "admin";
  shell.innerHTML = `
    <aside class="side-nav">
      <a class="brand" href="${isAdmin ? "index.html" : "concerts.html"}"><img src="../assets/audio-lines-light.svg" width="26" height="26" alt="" /><span class="brand__name brand__name--light">JUNG PA!</span></a>
      <p class="side-nav__label">BACKSTAGE / ${isAdmin ? "ADMIN" : "ORGANIZER"}</p>
      <nav class="side-nav__menu" aria-label="Admin">
        ${NAV.map(([k, label, href]) => href
          ? `<a class="side-nav__item${k === page ? " is-active" : ""}" href="${href}"${k === page ? ' aria-current="page"' : ""}>${svg(k)}${label}</a>`
          : `<span class="side-nav__item is-off" title="Needs a list-payments API in the backend">${svg(k)}${label}<small>SOON</small></span>`).join("")}
      </nav>
      <div class="side-nav__tools"><a href="/">↗&nbsp;&nbsp; View public website</a></div>
      <div class="side-nav__me">
        <span class="side-nav__avatar">${esc(initials)}</span>
        <span><b>${esc(me.first_name || (isAdmin ? "Admin" : "Organizer"))}</b><small>${isAdmin ? "Administrator" : "Organizer"}</small></span>
        <button type="button" id="logout">Log out</button>
      </div>
    </aside>
    <div class="work">
      <header class="toolbar">
        <span class="toolbar__crumb">Workspace / ${esc(TITLES[page] || "")}</span>
        <span class="toolbar__right" id="toolbar-right"><span class="badge badge--sale">Live workspace</span></span>
      </header>
    </div>`;
  document.body.prepend(shell);
  shell.querySelector(".work").append(content);
  content.hidden = false;
  shell.querySelector("#logout").addEventListener("click", () => { window.EncoreSession.clear(); location.href = "/"; });

  // ---------- data helpers ----------
  async function getJSON(path, opts = {}) {
    const res = await fetch(`${API_BASE}${path}`, { ...opts, headers: { Accept: "application/json", ...(opts.headers || {}) } });
    const body = await res.json().catch(() => ({}));
    if (!res.ok) { const err = new Error(body.detail || `HTTP ${res.status}`); err.status = res.status; err.body = body; err.res = res; throw err; }
    return body;
  }
  async function pool(items, limit, fn) {
    const queue = [...items];
    await Promise.all(Array.from({ length: Math.min(limit, queue.length) }, async () => {
      while (queue.length) { const it = queue.shift(); await fn(it).catch(() => null); }
    }));
  }

  // every event with its zones: list pages (100 each) + one detail call per event, cached briefly
  async function loadEvents({ fresh = false, onProgress } = {}) {
    if (!fresh) {
      try {
        const c = JSON.parse(sessionStorage.getItem(CACHE_KEY));
        if (c && Date.now() - c.at < CACHE_MS) return c.events;
      } catch { /* no cache */ }
    }
    const first = await getJSON(`/events?page=1&page_size=100`);
    let summaries = first.items;
    for (let p = 2; p <= first.total_pages; p++) summaries = summaries.concat((await getJSON(`/events?page=${p}&page_size=100`)).items);
    const out = [];
    let done = 0;
    await pool(summaries, PARALLEL, async (s) => {
      const d = await getJSON(`/events/${encodeURIComponent(s.id)}`);
      out.push(enrich(d));
      onProgress?.(++done, summaries.length);
    });
    out.sort((a, b) => String(a.date_time).localeCompare(String(b.date_time)));
    try { sessionStorage.setItem(CACHE_KEY, JSON.stringify({ at: Date.now(), events: out })); } catch { /* too big / blocked: fine */ }
    return out;
  }
  function enrich(e) {   // totals per event, computed from its zones
    const zones = e.zones || [];
    const capacity = zones.reduce((n, z) => n + (z.capacity || 0), 0);
    const booked = zones.reduce((n, z) => n + Math.min(z.booked_count || 0, z.capacity || 0), 0);
    const value = zones.reduce((n, z) => n + (z.booked_count || 0) * (z.price || 0), 0);
    return { ...e, capacity, booked, value, soldPct: capacity ? (booked / capacity) * 100 : 0 };
  }
  const title = (e) => (e.artist?.name && !String(e.title).includes(e.artist.name) ? `${e.artist.name} · ${e.title}` : e.title);

  // ---------- drawer ----------
  function openDrawer(headHtml, bodyHtml) {
    closeDrawer();
    const scrim = document.createElement("div");
    scrim.className = "scrim";
    const d = document.createElement("aside");
    d.className = "drawer";
    d.setAttribute("role", "dialog");
    d.setAttribute("aria-modal", "true");
    d.innerHTML = `<div class="drawer__head"><div>${headHtml}</div><button type="button" class="icon-x" aria-label="Close">×</button></div><div class="drawer__body">${bodyHtml}</div>`;
    document.body.append(scrim, d);
    requestAnimationFrame(() => d.classList.add("is-open"));
    scrim.addEventListener("click", closeDrawer);
    d.querySelector(".icon-x").addEventListener("click", closeDrawer);
    d.querySelector(".icon-x").focus();
    return d.querySelector(".drawer__body");
  }
  function closeDrawer() { document.querySelectorAll(".drawer, .scrim").forEach((n) => n.remove()); }
  document.addEventListener("keydown", (e) => { if (e.key === "Escape") closeDrawer(); });

  // ---------- CSV export ----------
  function downloadCSV(name, rows) {
    const csv = rows.map((r) => r.map((v) => `"${String(v ?? "").replace(/"/g, '""')}"`).join(",")).join("\r\n");
    const a = document.createElement("a");
    a.href = URL.createObjectURL(new Blob(["\ufeff" + csv], { type: "text/csv;charset=utf-8" }));   // BOM so Excel reads Thai text
    a.download = name;
    a.click();
    setTimeout(() => URL.revokeObjectURL(a.href), 1000);
  }

  // ---------- admin token (only for /admin/* APIs) ----------
  const token = {
    get() { try { return sessionStorage.getItem(TOKEN_KEY) || ""; } catch { return ""; } },
    set(v) { try { v ? sessionStorage.setItem(TOKEN_KEY, v) : sessionStorage.removeItem(TOKEN_KEY); } catch { /* ignore */ } },
  };

  window.Admin = { me, esc, money, num, asDate, fmtDate, getJSON, pool, loadEvents, title, openDrawer, closeDrawer, downloadCSV, token, toolbarRight: document.getElementById("toolbar-right") };
})();
