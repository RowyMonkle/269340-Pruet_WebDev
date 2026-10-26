/* ENCORE homepage: loads concerts from GET /api/v1/events and renders the dynamic sections.
 * Served by FastAPI at /test-client/encore/, so the API is on the same origin.
 * If the API is down or empty, the page falls back to the sample concerts from the Figma design.
 */
(() => {
  "use strict";

  // ---------- settings ----------
  const API_BASE = "/api/v1";
  const PAGE_SIZE = 100;                 // one request for the whole catalogue slice the homepage needs
  const CURRENCY = "฿";                  // the design shows £; the seed data prices are in baht
  const BUDGET_LIMIT = 2000;             // "Under ฿2,000" chip
  const SELLING_FAST_SEATS = 50;         // seats_left at or below this -> "Selling fast"
  const GRID_SIZE = 4;
  const UPCOMING_SIZE = 3;
  const ARTIST_SIZE = 5;
  const CONCERT_PHOTOS = ["assets/concert-1.png", "assets/concert-2.png", "assets/concert-3.png", "assets/concert-4.png"];
  const ARTIST_PHOTOS = ["assets/artist-1.png", "assets/artist-2.png", "assets/artist-3.png", "assets/artist-4.png", "assets/artist-5.png"];

  // Sample data from the Figma frame, used only when the API cannot be reached or has no events.
  const FALLBACK_EVENTS = [
    ev("f1", "Luna Vale", "Afterglow Tour", "Indie pop", "The O2", "London", "2026-11-14T19:30", 1450, 32),
    ev("f2", "The Midnight", "Neon Skyline Live", "Synthwave", "Alexandra Palace", "London", "2026-11-20T20:00", 1350, 400),
    ev("f3", "Maya Sol", "Golden Hour Sessions", "R&B / Soul", "Roundhouse", "London", "2026-11-28T19:00", 1200, 600),
    ev("f4", "Atlas & the Waves", "Everything in Motion", "Indie rock", "OVO Arena Wembley", "London", "2026-12-04T19:30", 1750, 900),
    ev("f5", "Florence Grey", "Wild Hearts Tour", "Alternative", "Royal Albert Hall", "London", "2027-01-08T19:30", 2100, 1000, "coming_soon"),
    ev("f6", "Solstice", "A Night in Orbit", "Electronic", "The O2", "London", "2027-01-16T20:00", 1550, 1000, "coming_soon"),
    ev("f7", "Noah Rivers", "Close to Home", "Pop", "Eventim Apollo", "London", "2027-01-29T19:00", 1100, 1000, "coming_soon"),
  ];
  function ev(id, artist, tour, genre, venue, city, date, price, seats, status = "on_sale") {
    return { id, title: `${artist} — ${tour}`, category: "concert", artist: { name: artist, genre },
             venue: { name: venue, city }, date_time: date, status, min_price: price, seats_left: seats };
  }

  // ---------- state ----------
  const state = {
    events: [],
    usingFallback: false,
    chip: "all",
    query: "", city: "", date: "", genre: "",
    heroIndex: 0,
    saved: new Set(readStore("encore.saved")),
    followed: new Set(readStore("encore.followed")),
    reminders: new Set(readStore("encore.reminders")),
  };

  // ---------- helpers ----------
  const $ = (sel) => document.querySelector(sel);
  const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));

  function readStore(key) {
    try { return JSON.parse(localStorage.getItem(key)) || []; } catch { return []; }
  }
  function writeStore(key, set) {
    try { localStorage.setItem(key, JSON.stringify([...set])); } catch { /* storage blocked: keep in memory only */ }
  }

  function splitTitle(e) {
    // "Artist — Tour" -> { main: "Artist", sub: "Tour" }; falls back to artist.name / title
    const artist = e.artist?.name;
    const parts = String(e.title || "").split(/\s+[—–-]\s+/);
    if (parts.length > 1) return { main: parts[0], sub: parts.slice(1).join(" — ") };
    if (artist && e.title && artist !== e.title) return { main: artist, sub: e.title };
    return { main: e.title || artist || "Untitled concert", sub: e.artist?.genre || "" };
  }
  const dateOf = (e) => (e.date_time ? new Date(e.date_time) : null);
  const valid = (d) => d && !Number.isNaN(d.getTime());

  function fmtCardDate(d) {
    if (!valid(d)) return "DATE TBA";
    const day = d.toLocaleDateString("en-GB", { weekday: "short" });
    const date = d.toLocaleDateString("en-GB", { day: "2-digit", month: "short", year: "numeric" });
    const time = d.toLocaleTimeString("en-GB", { hour: "2-digit", minute: "2-digit" });
    return `${day}, ${date} · ${time}`.toUpperCase();
  }
  function fmtShortDate(d) {
    return valid(d) ? d.toLocaleDateString("en-GB", { day: "2-digit", month: "short", year: "numeric" }).toUpperCase() : "DATE TBA";
  }
  const fmtPrice = (n) => (n == null ? "Price TBA" : `From ${CURRENCY}${Number(n).toLocaleString("en-US", { maximumFractionDigits: 0 })}`);
  const venueLine = (e) => [e.venue?.name, e.venue?.city].filter(Boolean).join(" · ");

  function availability(e) {
    // "upcoming" in the seed data = event not held yet and bookable, so it counts as on sale.
    // Only an explicit not-yet-on-sale status is "Coming soon".
    if (/coming[_ -]?soon|presale|draft|announced/i.test(e.status || "")) return { cls: "soon", label: "Coming soon" };
    if (/cancel/i.test(e.status || "")) return { cls: "out", label: "Cancelled" };
    if (e.seats_left === 0) return { cls: "out", label: "Sold out" };
    if (e.seats_left != null && e.seats_left <= SELLING_FAST_SEATS) return { cls: "fast", label: "Selling fast" };
    return { cls: "sale", label: "On sale" };
  }
  const isOnSale = (e) => !["soon"].includes(availability(e).cls);

  function isThisWeekend(d) {
    if (!valid(d)) return false;
    const today = new Date(); today.setHours(0, 0, 0, 0);
    const day = today.getDay();                                // 0 = Sunday ... 6 = Saturday
    const sat = new Date(today); sat.setDate(today.getDate() + (day === 0 ? -1 : 6 - day));  // this weekend's Saturday
    const monday = new Date(sat); monday.setDate(sat.getDate() + 2);
    return d >= sat && d < monday;
  }
  const withinDays = (d, days) => valid(d) && d >= new Date() && d <= new Date(Date.now() + days * 864e5);

  function toast(msg) {
    let t = $(".toast");
    if (!t) { t = document.createElement("div"); t.className = "toast"; t.setAttribute("role", "status"); document.body.append(t); }
    t.textContent = msg;
    t.classList.add("is-visible");
    clearTimeout(toast.timer);
    toast.timer = setTimeout(() => t.classList.remove("is-visible"), 2600);
  }

  // ---------- data ----------
  async function loadEvents() {
    try {
      const res = await fetch(`${API_BASE}/events?page=1&page_size=${PAGE_SIZE}`, { headers: { Accept: "application/json" } });
      if (!res.ok) throw new Error(`HTTP ${res.status}`);
      const body = await res.json();
      const items = Array.isArray(body) ? body : body.items || [];
      if (!items.length) throw new Error("no events yet");
      state.events = items;
    } catch (err) {
      console.warn("[ENCORE] using the sample concerts from the design:", err.message);
      state.events = FALLBACK_EVENTS;
      state.usingFallback = true;
    }
    state.events.sort((a, b) => (dateOf(a)?.getTime() || Infinity) - (dateOf(b)?.getTime() || Infinity));
  }

  function filtered() {
    const q = state.query.trim().toLowerCase();
    return state.events.filter((e) => {
      const d = dateOf(e);
      if (!isOnSale(e)) return false;
      if (q && ![e.title, e.artist?.name, e.venue?.name, e.venue?.city].some((v) => String(v || "").toLowerCase().includes(q))) return false;
      if (state.city && e.venue?.city !== state.city) return false;
      if (state.genre && !String(e.artist?.genre || e.category || "").toLowerCase().includes(state.genre.toLowerCase())) return false;
      if (state.date === "weekend" && !isThisWeekend(d)) return false;
      if (state.date === "month" && !withinDays(d, 30)) return false;
      switch (state.chip) {
        case "foryou": return state.followed.size === 0 || state.followed.has(e.artist?.name);
        case "weekend": return isThisWeekend(d);
        case "budget": return e.min_price != null && e.min_price < BUDGET_LIMIT;
        case "fast": return availability(e).cls === "fast";
        default: return true;
      }
    });
  }

  // ---------- rendering ----------
  function renderHero() {
    const featured = state.events.filter(isOnSale).slice(0, 3);
    if (!featured.length) return;
    state.heroIndex = (state.heroIndex + featured.length) % featured.length;
    const e = featured[state.heroIndex];
    const { main, sub } = splitTitle(e);
    $("#hero-teaser-title").textContent = sub ? `${main} — ${sub}` : main;
    $("#hero-teaser-title").href = eventHref(e);
    $("#hero-teaser-when").textContent = [fmtShortDate(dateOf(e)), [e.venue?.name, e.venue?.city].filter(Boolean).join(", ").toUpperCase()].filter(Boolean).join(" · ");
    $("#hero-count").textContent = `${String(state.heroIndex + 1).padStart(2, "0")} / ${String(featured.length).padStart(2, "0")}`;
    $("#hero-artist-btn").textContent = `Meet ${e.artist?.name || main}`;
  }

  const eventHref = (e) => (state.usingFallback ? "#concerts" : `event.html?id=${encodeURIComponent(e.id)}`);

  function renderGrid() {
    const list = filtered().slice(0, GRID_SIZE);
    $("#concert-empty").hidden = list.length > 0;
    $("#concert-grid").innerHTML = list.map((e, i) => {
      const { main, sub } = splitTitle(e);
      const a = availability(e);
      const saved = state.saved.has(String(e.id));
      return `
        <article class="card">
          <div class="card__art">
            <img src="${CONCERT_PHOTOS[i % CONCERT_PHOTOS.length]}" alt="" loading="lazy" />
            <span class="badge badge--${a.cls} card__badge">${a.label}</span>
            <button type="button" class="save" data-save="${esc(e.id)}" aria-pressed="${saved}" aria-label="${saved ? "Remove from saved" : "Save"} ${esc(main)}">
              <img src="assets/heart.svg" width="16" height="16" alt="" />
            </button>
          </div>
          <p class="card__date">${fmtCardDate(dateOf(e))}</p>
          <div>
            <h3 class="card__title">${esc(main)}</h3>
            ${sub ? `<p class="card__sub">${esc(sub)}</p>` : ""}
          </div>
          <p class="card__venue">${esc(venueLine(e))}</p>
          <div class="card__foot">
            <span class="card__price">${fmtPrice(e.min_price)}</span>
            <a class="card__cta" href="${eventHref(e)}">Find tickets ↗</a>
          </div>
        </article>`;
    }).join("");
  }

  function renderUpcoming() {
    const shown = new Set(filtered().slice(0, GRID_SIZE).map((e) => String(e.id)));
    const soon = state.events.filter((e) => !isOnSale(e));
    const later = state.events.filter((e) => isOnSale(e) && !shown.has(String(e.id)));
    const list = [...soon, ...later].slice(0, UPCOMING_SIZE);
    $("#upcoming-list").innerHTML = list.map((e) => {
      const d = dateOf(e);
      const { main, sub } = splitTitle(e);
      const a = availability(e);
      const on = state.reminders.has(String(e.id));
      return `
        <li class="up">
          <div class="up__date">
            <span class="up__day">${valid(d) ? String(d.getDate()).padStart(2, "0") : "--"}</span>
            <span class="up__month">${valid(d) ? d.toLocaleDateString("en-GB", { month: "short", year: "numeric" }).toUpperCase() : "TBA"}</span>
          </div>
          <div class="up__info">
            <p class="up__title">${esc(sub ? `${main} · ${sub}` : main)}</p>
            <p class="up__meta">${esc([venueLine(e), fmtPrice(e.min_price)].filter(Boolean).join(" · "))}</p>
          </div>
          <div class="up__sale">
            <span class="badge badge--${a.cls}">${a.label}</span>
            <small>${valid(d) ? `${d.toLocaleDateString("en-GB", { weekday: "long" }).toUpperCase()} · ${d.toLocaleTimeString("en-GB", { hour: "2-digit", minute: "2-digit" })}` : ""}</small>
          </div>
          <button type="button" class="btn btn--outline" data-remind="${esc(e.id)}" aria-pressed="${on}">
            ${on ? "Reminder set" : "Remind me"} <img src="assets/bell.svg" width="16" height="16" alt="" />
          </button>
        </li>`;
    }).join("");
  }

  function renderArtists() {
    const seen = new Map();
    for (const e of state.events) {
      const name = e.artist?.name;
      if (name && !seen.has(name)) seen.set(name, e.artist?.genre || "Live music");
    }
    const artists = [...seen].slice(0, ARTIST_SIZE);
    $("#artist-grid").innerHTML = artists.map(([name, genre], i) => {
      const on = state.followed.has(name);
      return `
        <li class="artist">
          <img src="${ARTIST_PHOTOS[i % ARTIST_PHOTOS.length]}" alt="" loading="lazy" />
          <p class="artist__name">${esc(name)}</p>
          <p class="artist__meta">${esc(genre)} &nbsp; · &nbsp; <button type="button" class="follow" data-follow="${esc(name)}" aria-pressed="${on}">${on ? "✓ Following" : "+ Follow"}</button></p>
        </li>`;
    }).join("");
  }

  function fillSelect(sel, values) {
    const el = $(sel);
    const first = el.options[0].outerHTML;
    el.innerHTML = first + [...new Set(values.filter(Boolean))].sort().map((v) => `<option>${esc(v)}</option>`).join("");
  }

  function renderSkeleton() {
    $("#concert-grid").innerHTML = Array.from({ length: GRID_SIZE }, () => `
      <div class="card card--skeleton" aria-hidden="true">
        <div class="card__art"></div><div class="line" style="width:40%"></div>
        <div class="line" style="width:70%;height:20px"></div><div class="line" style="width:55%"></div>
      </div>`).join("");
  }

  function renderAll() { renderHero(); renderGrid(); renderUpcoming(); renderArtists(); }

  // ---------- interactions ----------
  function setChip(chip) {
    state.chip = chip;
    document.querySelectorAll("#chips .chip").forEach((b) => {
      const on = b.dataset.chip === chip;
      b.classList.toggle("is-active", on);
      b.setAttribute("aria-pressed", on);
    });
    renderGrid(); renderUpcoming();
  }

  function setGenre(genre) {
    state.genre = state.genre === genre ? "" : genre;
    document.querySelectorAll("#genre-list .genre").forEach((b) => b.classList.toggle("is-active", b.dataset.genre === state.genre));
    const sel = $("#search-genre");
    if ([...sel.options].some((o) => o.value === state.genre)) sel.value = state.genre;
    renderGrid(); renderUpcoming();
    $("#concerts").scrollIntoView({ behavior: "smooth" });
  }

  function bind() {
    $("#hero-prev").addEventListener("click", () => { state.heroIndex--; renderHero(); });
    $("#hero-next").addEventListener("click", () => { state.heroIndex++; renderHero(); });

    $("#search-form").addEventListener("submit", (e) => {
      e.preventDefault();
      state.query = $("#search-q").value;
      state.city = $("#search-city").value;
      state.date = $("#search-date").value;
      state.genre = $("#search-genre").value;
      document.querySelectorAll("#genre-list .genre").forEach((b) => b.classList.toggle("is-active", b.dataset.genre === state.genre));
      renderGrid(); renderUpcoming();
      $("#concerts").scrollIntoView({ behavior: "smooth" });
    });

    $("#chips").addEventListener("click", (e) => {
      const b = e.target.closest("[data-chip]");
      if (b) setChip(b.dataset.chip);
    });
    $("#view-all").addEventListener("click", () => {
      state.query = state.city = state.date = state.genre = "";
      $("#search-form").reset();
      document.querySelectorAll("#genre-list .genre").forEach((b) => b.classList.remove("is-active"));
      setChip("all");
    });
    $("#genre-list").addEventListener("click", (e) => {
      const b = e.target.closest("[data-genre]");
      if (b) setGenre(b.dataset.genre);
    });

    document.addEventListener("click", (e) => {
      const save = e.target.closest("[data-save]");
      if (save) {
        const id = save.dataset.save;
        state.saved.has(id) ? state.saved.delete(id) : state.saved.add(id);
        writeStore("encore.saved", state.saved);
        renderGrid();
        toast(state.saved.has(id) ? "Saved to your list" : "Removed from your list");
        return;
      }
      const remind = e.target.closest("[data-remind]");
      if (remind) {
        const id = remind.dataset.remind;
        state.reminders.has(id) ? state.reminders.delete(id) : state.reminders.add(id);
        writeStore("encore.reminders", state.reminders);
        renderUpcoming();
        toast(state.reminders.has(id) ? "We’ll remind you before tickets go on sale" : "Reminder removed");
        return;
      }
      const follow = e.target.closest("[data-follow]");
      if (follow) {
        const name = follow.dataset.follow;
        state.followed.has(name) ? state.followed.delete(name) : state.followed.add(name);
        writeStore("encore.followed", state.followed);
        renderArtists();
        if (state.chip === "foryou") renderGrid();
        toast(state.followed.has(name) ? `Following ${name}` : `Unfollowed ${name}`);
      }
    });

    $("#signup-form").addEventListener("submit", (e) => {
      e.preventDefault();
      const input = $("#signup-email");
      const note = $("#signup-note");
      const ok = input.checkValidity() && input.value.trim() !== "";
      input.setAttribute("aria-invalid", String(!ok));
      note.classList.toggle("is-error", !ok);
      note.textContent = ok
        ? "You’re on the list. Watch your inbox for the first announcement."
        : "Please enter a valid email address.";
      if (ok) e.target.reset();
    });
  }

  // ---------- start ----------
  async function init() {
    bind();
    renderSkeleton();
    await loadEvents();
    fillSelect("#search-city", state.events.map((e) => e.venue?.city));
    fillSelect("#search-genre", state.events.map((e) => e.artist?.genre));
    const budget = $("#chip-budget");
    if (budget) budget.textContent = `Under ${CURRENCY}${BUDGET_LIMIT.toLocaleString("en-US")}`;
    renderAll();
  }
  init();
})();
