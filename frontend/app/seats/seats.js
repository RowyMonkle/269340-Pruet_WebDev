/* ENCORE seat selection: /seats/seats.html?id=<event id>[&zone=<zone name>]
 *
 * 1. GET /api/v1/events/{id}      -> zones (name, price, capacity, booked_count)
 * 2. the fan picks a zone, then seats (max MAX_TICKETS)
 * 3. "Continue to checkout" -> POST /api/v1/orders with an Idempotency-Key
 *    (holds the seats for 10 minutes) -> /checkout/checkout.html?order=<order id>
 *
 * Seat-level availability: the API only exposes booked_count per zone, not WHICH seats are taken,
 * so taken seats are spread deterministically over the zone in proportion to booked_count.
 * The database is still the judge: the unique index uq_tickets_event_zone_seat answers 409 for a
 * seat that is really taken, and this page then greys that seat out and asks for another one.
 */
(() => {
  "use strict";

  // ---------- settings ----------
  const API_BASE = "/api/v1";
  const CURRENCY = "฿";
  const MAX_TICKETS = 6;              // per order
  const SEATS_PER_ROW = 18;           // as in the Figma seat grid
  const ROWS_PER_PAGE = 6;            // big zones are paged 6 rows at a time
  const PAYMENT_METHOD = "promptpay"; // default; the fan can change it at checkout
  const ZOOM_STEPS = [0.8, 0.9, 1, 1.15, 1.3];

  const $ = (sel) => document.querySelector(sel);
  const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
  const money = (n, dp = 0) => `${CURRENCY}${Number(n).toLocaleString("en-US", { minimumFractionDigits: dp, maximumFractionDigits: dp })}`;
  const plural = (n, w) => `${n} ${w}${n === 1 ? "" : "s"}`;

  const params = new URLSearchParams(location.search);
  const eventId = params.get("id");
  const STORE_KEY = `encore.seats.${eventId}`;   // sessionStorage: keep the picks when the fan goes back and forth

  const state = {
    event: null,
    zones: [],             // sorted dearest first, each with .tone and .left
    zone: null,            // zone being viewed
    rowPage: 0,
    zoom: 2,               // index in ZOOM_STEPS (100%)
    selected: new Map(),   // key "zone|seat" -> { zone, seat, price }
    taken: new Set(readSession(`${STORE_KEY}.taken`)),   // seats the server told us are gone (409)
    idemKey: null,         // Idempotency-Key for the current selection
  };

  // ---------- storage helpers (never let a blocked storage break the page) ----------
  function readSession(key) {
    try { return JSON.parse(sessionStorage.getItem(key)) || []; } catch { return []; }
  }
  function writeSession(key, value) {
    try { sessionStorage.setItem(key, JSON.stringify(value)); } catch { /* ignore */ }
  }
  function currentUserId() {
    return Number(window.EncoreSession?.get()?.id) || null;   // set by login.html / register.html (session.js)
  }
  function newIdempotencyKey() {
    return (crypto.randomUUID && crypto.randomUUID()) || `seat-${Date.now()}-${Math.random().toString(16).slice(2)}`;
  }

  // ---------- seat maths ----------
  function rowName(i) {   // 0 -> A, 25 -> Z, 26 -> AA
    let s = "";
    for (i += 1; i > 0; i = Math.floor((i - 1) / 26)) s = String.fromCharCode(65 + ((i - 1) % 26)) + s;
    return s;
  }
  function hash(str) {    // small stable hash (FNV-1a) -> 0..1
    let h = 2166136261;
    for (let i = 0; i < str.length; i++) { h ^= str.charCodeAt(i); h = Math.imul(h, 16777619); }
    return (h >>> 0) / 4294967295;
  }
  function isTaken(zone, seat) {
    if (state.taken.has(`${zone.name}|${seat}`)) return true;
    const ratio = zone.capacity ? zone.booked_count / zone.capacity : 1;
    return hash(`${state.event.id}|${zone.name}|${seat}`) < ratio;
  }
  const seatKey = (zone, seat) => `${zone.name}|${seat}`;

  // ---------- load ----------
  async function load() {
    $("#seats-error").hidden = true;
    $("#workspace").hidden = false;
    if (!eventId) return showError(400);
    try {
      const res = await fetch(`${API_BASE}/events/${encodeURIComponent(eventId)}`, { headers: { Accept: "application/json" } });
      if (!res.ok) return showError(res.status);
      state.event = await res.json();
      init();
    } catch {
      showError(0);
    } finally {
      $("#booking").removeAttribute("aria-busy");
    }
  }

  function showError(status) {
    const retry = status === 0 || status >= 500;
    $("#error-title").textContent = retry ? "We can’t load the seat map right now." : "We couldn’t find that concert.";
    $("#error-text").textContent = retry
      ? "The ticket service is busy. Please try again in a moment."
      : "The link may be old or the concert may have been removed.";
    $("#retry-btn").hidden = !retry;
    $("#seats-error").hidden = false;
    $("#workspace").hidden = true;
    $("#hold-badge").hidden = true;
  }

  function splitTitle(e) {   // same rule as the other pages
    const artist = e.artist?.name;
    const parts = String(e.title || "").split(/\s+[—–-]\s+/);
    if (parts.length > 1) return { main: parts[0], sub: parts.slice(1).join(" — ") };
    if (artist && e.title && artist !== e.title) return { main: artist, sub: e.title };
    return { main: e.title || artist || "Untitled concert", sub: "" };
  }

  // ---------- first render ----------
  function init() {
    const e = state.event;
    const { main, sub } = splitTitle(e);
    const d = e.date_time ? new Date(/[zZ]|[+-]\d\d:\d\d$/.test(e.date_time) ? e.date_time : e.date_time + "Z") : null;   // UTC from the API -> local time
    const ok = d && !Number.isNaN(d.getTime());
    const when = ok
      ? `${d.toLocaleDateString("en-GB", { weekday: "short", day: "numeric", month: "short", year: "numeric" })} · ${d.toLocaleTimeString("en-GB", { hour: "2-digit", minute: "2-digit" })}`
      : "Date to be announced";
    const where = [e.venue?.name, e.venue?.city].filter(Boolean).join(", ");

    document.title = `Choose seats · ${main} | ENCORE`;
    $("#event-meta").innerHTML =
      `${esc([main, sub].filter(Boolean).join(" · "))}  /  ${esc(when)}  /  ${esc(where)} · ` +
      `<a href="../event/event.html?id=${encodeURIComponent(e.id)}">Concert details</a>`;
    $("#sum-name").textContent = [main, sub].filter(Boolean).join(" · ");
    $("#sum-when").textContent = when;
    $("#sum-where").textContent = where;
    $("#max-tickets").textContent = MAX_TICKETS;
    window.EncoreArt?.apply(document.querySelector(".summary__photo"), e.artist?.name || main);

    const onSale = !/coming[_ -]?soon|presale|cancel|complete|past|ended|draft/i.test(e.status || "");
    state.zones = [...(e.zones || [])]
      .sort((a, b) => b.price - a.price)
      .map((z, i) => ({ ...z, tone: i % 5, left: onSale ? Math.max(0, z.capacity - z.booked_count) : 0 }));

    state.selected = new Map();   // init() also runs again after a reload of the counts
    // restore picks from this tab (dropping any that no longer exist or were taken meanwhile)
    for (const p of readSession(STORE_KEY)) {
      const z = state.zones.find((x) => x.name === p.zone);
      if (z && !isTaken(z, p.seat) && state.selected.size < MAX_TICKETS) state.selected.set(seatKey(z, p.seat), { zone: z, seat: p.seat, price: z.price });
    }

    state.arena = Boolean(window.EncoreArena?.fits(state.zones));   // IMPACT Arena plan, else the simple block layout
    state.locked = !onSale;
    state.restricted = new Set(e.dynamic_attributes?.restricted_view_zones || []);
    document.querySelector(".arena").hidden = state.arena;
    $("#arena-svg").hidden = !state.arena;
    renderZones();
    renderCategories();
    const wanted = state.zones.find((z) => z.name === params.get("zone") && z.left > 0)
      || state.zones.find((z) => z.left > 0);
    if (wanted) selectZone(wanted);
    else $("#seat-grid").innerHTML = `<p class="seatgrid__empty">${onSale ? "This concert is sold out." : "Tickets aren’t on sale for this concert."}</p>`;
    renderSummary();
  }

  // ---------- venue overview ----------
  function renderZones() {
    if (state.arena) {
      window.EncoreArena.render($("#arena-svg"), state.zones, {
        selected: state.zone?.name,
        highlightPrice: state.tier,
        locked: state.locked,
        onPick: (name) => {
          const z = state.zones.find((x) => x.name === name);
          if (!z || z.left <= 0) return;
          state.tier = null;   // a click on the plan clears the tier filter
          selectZone(z);
          $("#zone-pick").scrollIntoView({ behavior: "smooth", block: "nearest" });
        },
      });
      return;
    }
    // dearest zone in the centre at the stage, next two on the sides, the rest as bands further back
    const z = state.zones;
    const order = [z[1], z[0], z[2], ...z.slice(3)].filter(Boolean);
    $("#arena-zones").innerHTML = order.map((zone) => {
      const i = z.indexOf(zone);
      const pos = i === 0 ? "centre" : i <= 2 ? "side" : "band";
      const width = pos === "band" ? `style="--band-w:${Math.max(40, 80 - (i - 3) * 20)}%"` : "";
      const picked = [...state.selected.values()].filter((s) => s.zone === zone).length;
      return `<button type="button" class="zone-btn zone-btn--${pos} tone-${zone.tone}" ${width}
          data-zone="${i}" aria-pressed="${state.zone === zone}" ${zone.left === 0 ? "disabled" : ""}
          aria-label="${esc(zone.name)}, ${money(zone.price)}, ${zone.left === 0 ? "sold out" : plural(zone.left, "seat") + " left"}">
          <b>${esc(zone.name)}</b>
          <span>${zone.left === 0 ? "Sold out" : money(zone.price)}</span>
          ${picked ? `<small>${picked} selected</small>` : ""}
        </button>`;
    }).join("");
    // centre zone sits in the middle column even when there is no left zone
    if (z.length === 1) $("#arena-zones .zone-btn").style.gridColumn = "2";
  }

  function renderCategories() {
    if (state.arena) {
      // one card per price tier (the plan has ~60 zones): click to highlight that tier on the map
      $("#categories").innerHTML = window.EncoreArena.tiers(state.zones).map((t) => {
        const left = t.zones.reduce((n, z) => n + z.left, 0);
        const names = t.zones.map((z) => z.name.replace(" Rear", "↑"));
        const current = state.zone && state.zone.price === t.price;   // tier of the section picked on the plan
        return `<button type="button" class="cat${current ? " is-current" : ""}" data-tier="${t.price}" aria-pressed="${state.tier === t.price}" ${left === 0 ? "disabled" : ""}>
          <span class="cat__label"><i style="background:${t.color}"></i>${esc(names.length > 6 ? names.slice(0, 6).join(", ") + " …" : names.join(", "))}</span>
          <span class="cat__price">${left === 0 ? "Sold out" : money(t.price)}</span>
          <small class="cat__left">${left === 0 ? "" : `${left.toLocaleString("en-US")} seats left`}</small>
        </button>`;
      }).join("");
      return;
    }
    $("#categories").innerHTML = state.zones.map((zone, i) => `
      <button type="button" class="cat" data-zone="${i}" aria-pressed="${state.zone === zone}" ${zone.left === 0 ? "disabled" : ""}>
        <span class="cat__label"><i class="tone-${zone.tone}"></i>${esc(zone.name)}</span>
        <span class="cat__price">${zone.left === 0 ? "Sold out" : money(zone.price)}</span>
      </button>`).join("");
  }

  function selectZone(zone) {
    state.zone = zone;
    state.rowPage = 0;
    renderZones();
    renderCategories();
    renderGrid();
    $("#zone-badge").textContent = `${zone.name} selected`;
    // price box under the plan + line in the order summary
    const tier = state.arena ? window.EncoreArena.tiers(state.zones).find((t) => t.price === zone.price) : null;
    const level = state.arena ? window.EncoreArena.levelOf(zone.name) : "";
    $("#zone-pick").classList.add("is-set");
    $("#pick-swatch").style.background = tier ? tier.color : "var(--plum-700)";
    $("#pick-name").textContent = `Section ${zone.name}${level ? ` · ${level}` : ""}`;
    $("#pick-meta").textContent = `${plural(zone.left, "seat")} left`
      + (state.restricted?.has(zone.name) ? " · Restricted view" : "")
      + " · pick your seats below";
    $("#pick-price").innerHTML = `${money(zone.price)} <small>/ ticket</small>`;
    if (state.arena) $("#zone-title").textContent = `Seats in section ${zone.name}`;   // price is in the box above
    $("#sum-zone").hidden = false;
    $("#sum-zone").textContent = `Selected section: ${zone.name} · ${money(zone.price)} per ticket`;
    $("#zone-title").textContent = `${zone.name} · ${money(zone.price)} per ticket · ${plural(zone.left, "seat")} left`
      + (state.restricted?.has(zone.name) ? " · Restricted view" : "")
      + (/ Rear$/.test(zone.name) ? " · Rear rows of level 3" : "");
  }

  // ---------- seat grid ----------
  function renderGrid() {
    const zone = state.zone;
    const totalRows = Math.ceil(zone.capacity / SEATS_PER_ROW);
    const pages = Math.ceil(totalRows / ROWS_PER_PAGE);
    const first = state.rowPage * ROWS_PER_PAGE;
    const last = Math.min(totalRows, first + ROWS_PER_PAGE);
    const rows = [];
    for (let r = first; r < last; r++) {
      const name = rowName(r);
      const inRow = Math.min(SEATS_PER_ROW, zone.capacity - r * SEATS_PER_ROW);
      let cells = "";
      for (let n = 1; n <= inRow; n++) {
        const seat = `${name}-${n}`;
        const taken = isTaken(zone, seat);
        const picked = state.selected.has(seatKey(zone, seat));
        // the last two seats of the back row of each block are wheelchair spaces (mint in the design)
        const access = r === last - 1 && n > inRow - 2;
        cells += `<button type="button" role="gridcell" class="seat${access ? " seat--access" : ""}" data-seat="${seat}"
            aria-pressed="${picked}" ${taken ? "disabled" : ""}
            title="Row ${name} · Seat ${n}${taken ? " · taken" : ` · ${money(zone.price)}`}${access ? " · wheelchair space" : ""}"
            aria-label="Row ${name} seat ${n}${access ? ", wheelchair space" : ""}${taken ? ", taken" : picked ? ", selected" : ""}">${picked ? "✓" : n}</button>`;
        if (n === Math.ceil(inRow / 2) && inRow > 8) cells += `<span class="seatrow__aisle" aria-hidden="true"></span>`;   // aisle in the middle
      }
      rows.push(`<div class="seatrow" role="row"><span class="seatrow__label">${name}</span>${cells}<span class="seatrow__label">${name}</span></div>`);
    }
    const grid = $("#seat-grid");
    grid.innerHTML = rows.join("");
    // seat outline in the colour of the zone's price tier (same colours as the plan)
    const tier = state.arena ? window.EncoreArena.tiers(state.zones).find((t) => t.price === zone.price) : null;
    grid.style.setProperty("--tier", tier ? tier.color : "var(--plum-700)");
    grid.style.transform = `scale(${ZOOM_STEPS[state.zoom]})`;
    grid.style.marginBottom = `${(ZOOM_STEPS[state.zoom] - 1) * grid.offsetHeight}px`;   // scale() does not move the content below

    $("#row-pager").hidden = pages <= 1;
    $("#rows-label").textContent = `Rows ${rowName(first)}–${rowName(last - 1)} of ${rowName(0)}–${rowName(totalRows - 1)}`;
    $("#rows-prev").disabled = state.rowPage === 0;
    $("#rows-next").disabled = state.rowPage >= pages - 1;
  }

  function toggleSeat(seat) {
    const zone = state.zone;
    const key = seatKey(zone, seat);
    if (state.selected.has(key)) {
      state.selected.delete(key);
    } else {
      if (state.selected.size >= MAX_TICKETS) return toast(`You can book up to ${MAX_TICKETS} tickets per order.`);
      state.selected.set(key, { zone, seat, price: zone.price });
    }
    selectionChanged();
  }

  function selectionChanged() {
    state.idemKey = null;   // a different set of seats is a different order
    writeSession(STORE_KEY, [...state.selected.values()].map((s) => ({ zone: s.zone.name, seat: s.seat })));
    $("#checkout-error").hidden = true;
    renderZones();
    if (state.zone) renderGrid();
    renderSummary();
  }

  // ---------- summary ----------
  function renderSummary() {
    const picks = [...state.selected.values()].sort((a, b) => b.price - a.price || a.seat.localeCompare(b.seat, "en", { numeric: true }));
    const total = picks.reduce((s, p) => s + p.price, 0);
    $("#ticket-count").textContent = plural(picks.length, "ticket");
    $("#sum-seats").innerHTML = picks.map((p) => {
      const [row, n] = p.seat.split("-");
      return `<li class="sumseat">
          <span class="sumseat__info"><b>${esc(p.zone.name)} · Row ${esc(row)} · Seat ${esc(n)}</b><small>${esc(p.seat)}</small></span>
          <span class="sumseat__end">${money(p.price, 2)}
            <button type="button" class="sumseat__remove" data-remove="${esc(seatKey(p.zone, p.seat))}" aria-label="Remove ${esc(p.zone.name)} ${esc(p.seat)}">×</button>
          </span>
        </li>`;
    }).join("");
    $("#sum-empty").hidden = picks.length > 0;
    $("#sum-clear").hidden = picks.length === 0;
    $("#clear-picks").disabled = picks.length === 0;
    $("#pick-count").innerHTML = picks.length
      ? `<b>${picks.length} of ${MAX_TICKETS}</b> seats picked · tap a picked seat (✓) to remove it`
      : "Tap a seat to add it. Tap it again to remove it.";
    // label "Tickets (2 × ฿85.00)" when every seat costs the same, else just "Tickets (3)"
    const same = picks.length && picks.every((p) => p.price === picks[0].price);
    $("#sum-tickets-label").textContent = picks.length
      ? `Tickets (${same ? `${picks.length} × ${money(picks[0].price, 2)}` : picks.length})`
      : "Tickets";
    $("#sum-tickets").textContent = money(total, 2);
    $("#sum-total").textContent = money(total, 2);
    $("#checkout-btn").disabled = picks.length === 0;
    $("#checkout-btn").firstChild.textContent = currentUserId() ? "Continue to checkout " : "Log in to continue ";
  }

  // ---------- checkout: POST /orders ----------
  async function checkout() {
    const picks = [...state.selected.values()];
    if (!picks.length) return;
    const userId = currentUserId();
    if (!userId) {
      // picks are already saved in sessionStorage, so they are still here after logging in
      location.href = window.EncoreSession.loginHref();
      return;
    }
    const btn = $("#checkout-btn");
    const err = $("#checkout-error");
    err.hidden = true;
    btn.disabled = true;
    btn.setAttribute("aria-busy", "true");
    state.idemKey ||= newIdempotencyKey();   // same key on a retry of the same seats -> no duplicate order

    try {
      const res = await fetch(`${API_BASE}/orders`, {
        method: "POST",
        headers: { "Content-Type": "application/json", Accept: "application/json", "Idempotency-Key": state.idemKey },
        body: JSON.stringify({
          user_id: userId,
          payment_method: PAYMENT_METHOD,
          // price is sent for display only; the server uses the MongoDB zone price
          items: picks.map((p) => ({ event_id: state.event.id, seat_zone: p.zone.name, seat_number: p.seat, price: p.price })),
        }),
      });
      const body = await res.json().catch(() => ({}));
      if (res.ok) {
        try { sessionStorage.removeItem(STORE_KEY); } catch { /* ignore */ }
        location.href = `../checkout/checkout.html?order=${encodeURIComponent(body.id)}`;
        return;
      }
      handleOrderError(res, body);
    } catch {
      showOrderError("We couldn’t reach the ticket service. Check your connection and try again.");
    } finally {
      btn.removeAttribute("aria-busy");
      btn.disabled = state.selected.size === 0;
    }
  }

  function handleOrderError(res, body) {
    const detail = typeof body.detail === "string" ? body.detail : "";
    if (res.status === 409 && /double-booking|already been reserved/i.test(detail)) {
      // somebody else holds at least one of these seats: grey out all of them and ask for new ones
      for (const p of state.selected.values()) state.taken.add(seatKey(p.zone, p.seat));
      writeSession(`${STORE_KEY}.taken`, [...state.taken]);
      state.selected.clear();
      selectionChanged();
      return showOrderError("Someone just took one of those seats. We’ve cleared your picks — please choose again.");
    }
    if (res.status === 409 && /sold out/i.test(detail)) {
      load();   // refresh counts
      return showOrderError("That section has just sold out. Please pick another section.");
    }
    if (res.status === 404 && /user/i.test(detail)) {   // the stored account no longer exists: log in again
      window.EncoreSession.clear();
      return showOrderError("Please log in again before booking.");
    }
    if (res.status === 404) return showOrderError("This concert is no longer available.");
    if (res.status === 503) {
      const wait = Number(res.headers.get("Retry-After")) || 2;
      return showOrderError(`Lots of fans are booking right now. Please try again in ${wait} seconds.`);
    }
    showOrderError(detail || "Something went wrong while holding your seats. Please try again.");
  }
  function showOrderError(msg) {
    const err = $("#checkout-error");
    err.textContent = msg;
    err.hidden = false;
  }

  // ---------- toast ----------
  function toast(msg) {
    let t = $(".toast");
    if (!t) { t = document.createElement("div"); t.className = "toast"; t.setAttribute("role", "status"); document.body.append(t); }
    t.textContent = msg;
    requestAnimationFrame(() => t.classList.add("is-visible"));
    clearTimeout(toast.timer);
    toast.timer = setTimeout(() => t.classList.remove("is-visible"), 2600);
  }

  // ---------- events (delegated so re-rendering needs no re-binding) ----------
  $("#arena-zones").addEventListener("click", (ev) => {
    const b = ev.target.closest("[data-zone]");
    if (b && !b.disabled) selectZone(state.zones[Number(b.dataset.zone)]);
  });
  $("#categories").addEventListener("click", (ev) => {
    const t = ev.target.closest("[data-tier]");
    if (t && !t.disabled) {   // tier card: toggle the highlight, jump to the tier's emptiest zone
      const price = Number(t.dataset.tier);
      state.tier = state.tier === price ? null : price;
      const best = state.tier == null ? null : state.zones.filter((z) => z.price === price && z.left > 0).sort((a, b) => b.left - a.left)[0];
      if (best) selectZone(best); else { renderZones(); renderCategories(); }
      return;
    }
    const b = ev.target.closest("[data-zone]");
    if (b && !b.disabled) selectZone(state.zones[Number(b.dataset.zone)]);
  });
  $("#seat-grid").addEventListener("click", (ev) => {
    const b = ev.target.closest("[data-seat]");
    if (b && !b.disabled) toggleSeat(b.dataset.seat);
  });
  const clearAll = () => { state.selected.clear(); selectionChanged(); toast("All picks removed"); };
  $("#clear-picks").addEventListener("click", clearAll);
  $("#sum-clear").addEventListener("click", clearAll);
  $("#sum-seats").addEventListener("click", (ev) => {
    const b = ev.target.closest("[data-remove]");
    if (b) { state.selected.delete(b.dataset.remove); selectionChanged(); }
  });
  $("#rows-prev").addEventListener("click", () => { state.rowPage--; renderGrid(); });
  $("#rows-next").addEventListener("click", () => { state.rowPage++; renderGrid(); });
  const setZoom = (i) => {
    state.zoom = Math.max(0, Math.min(ZOOM_STEPS.length - 1, i));
    $("#zoom-level").textContent = `${Math.round(ZOOM_STEPS[state.zoom] * 100)}%`;
    if (state.zone) renderGrid();
  };
  $("#zoom-out").addEventListener("click", () => setZoom(state.zoom - 1));
  $("#zoom-in").addEventListener("click", () => setZoom(state.zoom + 1));
  $("#zoom-full").addEventListener("click", () => {
    const plan = $(".plan");
    if (document.fullscreenElement) document.exitFullscreen();
    else plan.requestFullscreen?.().catch(() => toast("Full screen isn’t available here"));
  });
  $("#checkout-btn").addEventListener("click", checkout);
  $("#retry-btn").addEventListener("click", load);

  load();
})();
