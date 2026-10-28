/* ENCORE concert details: /event/event.html?id=<MongoDB id>
 * Loads GET /api/v1/events/{id} (full document: zones, artist bio, venue, dynamic_attributes)
 * and fills the page. Shares the localStorage key "encore.saved" with the homepage hearts.
 */
(() => {
  "use strict";

  // ---------- settings (keep in step with app.js) ----------
  const API_BASE = "/api/v1";
  const CURRENCY = "฿";              // seed prices are in baht
  const SELLING_FAST_SEATS = 50;     // seats left at or below this -> "Selling fast"
  const SAVED_KEY = "encore.saved";

  const $ = (sel) => document.querySelector(sel);
  const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
  const money = (n) => `${CURRENCY}${Number(n).toLocaleString("en-US", { maximumFractionDigits: 0 })}`;
  const seatsLeft = (z) => Math.max(0, (z.capacity || 0) - (z.booked_count || 0));

  const params = new URLSearchParams(location.search);
  const eventId = params.get("id");
  let event = null;
  let selectedZone = null;

  // ---------- small helpers ----------
  function readSaved() {
    try { return new Set(JSON.parse(localStorage.getItem(SAVED_KEY)) || []); } catch { return new Set(); }
  }
  function writeSaved(set) {
    try { localStorage.setItem(SAVED_KEY, JSON.stringify([...set])); } catch { /* storage blocked: ignore */ }
  }
  function toast(msg) {
    let t = $(".toast");
    if (!t) { t = document.createElement("div"); t.className = "toast"; t.setAttribute("role", "status"); document.body.append(t); }
    t.textContent = msg;
    requestAnimationFrame(() => t.classList.add("is-visible"));
    clearTimeout(toast.timer);
    toast.timer = setTimeout(() => t.classList.remove("is-visible"), 2600);
  }
  function splitTitle(e) {
    // same rule as the homepage cards: "Artist — Tour" or artist name + title
    const artist = e.artist?.name;
    const parts = String(e.title || "").split(/\s+[—–-]\s+/);
    if (parts.length > 1) return { main: parts[0], sub: parts.slice(1).join(" — ") };
    if (artist && e.title && artist !== e.title) return { main: artist, sub: e.title };
    return { main: e.title || artist || "Untitled concert", sub: e.artist?.genre || "" };
  }
  function availability(e, left) {
    // same labels as the homepage badges
    if (/coming[_ -]?soon|presale|draft|announced/i.test(e.status || "")) return { cls: "soon", label: "Coming soon" };
    if (/cancel/i.test(e.status || "")) return { cls: "out", label: "Cancelled" };
    if (/complete|past|ended/i.test(e.status || "")) return { cls: "out", label: "Event ended" };
    if (left === 0) return { cls: "out", label: "Sold out" };
    if (left <= SELLING_FAST_SEATS) return { cls: "fast", label: "Selling fast" };
    return { cls: "sale", label: "On sale" };
  }
  const humanKey = (k) => k.replace(/_/g, " ").replace(/^\w/, (c) => c.toUpperCase());
  function humanValue(v) {
    if (v === true) return "Yes";
    if (v === false) return "No";
    if (Array.isArray(v)) return v.join(", ");
    if (v && typeof v === "object") return Object.values(v).map(humanValue).join(" · ");
    return String(v);
  }

  // ---------- load ----------
  async function load() {
    $("#event-error").hidden = true;
    $("#event-body").hidden = false;
    document.querySelector(".ehero__social").hidden = false;
    if (!eventId) return showError(400);
    try {
      const res = await fetch(`${API_BASE}/events/${encodeURIComponent(eventId)}`, { headers: { Accept: "application/json" } });
      if (!res.ok) return showError(res.status);
      event = await res.json();
      render(event);
    } catch {
      showError(0);   // network down / nginx cannot reach the API
    } finally {
      $("#event").removeAttribute("aria-busy");
    }
  }

  function showError(status) {
    // 400 = bad id, 404 = not found: nothing to retry. 503 / network: the API is busy, offer a retry.
    const retry = status === 0 || status >= 500;
    $("#error-title").textContent = retry ? "We can’t load this concert right now." : "We couldn’t find that concert.";
    $("#error-text").textContent = retry
      ? "The ticket service is busy. Please try again in a moment."
      : "The link may be old or the concert may have been removed.";
    $("#retry-btn").hidden = !retry;
    $("#event-error").hidden = false;
    $("#event-body").hidden = true;
    $("#event-title").textContent = retry ? "Please try again" : "Concert not found";
    $("#crumb-city").textContent = "—";
    $("#crumb-name").textContent = "—";
    document.querySelector(".ehero__social").hidden = true;   // nothing to save or share
  }

  // ---------- render ----------
  function render(e) {
    const { main, sub } = splitTitle(e);
    const zones = [...(e.zones || [])].sort((a, b) => b.price - a.price);   // dearest first, as in the design
    const left = zones.reduce((n, z) => n + seatsLeft(z), 0);
    const open = zones.filter((z) => seatsLeft(z) > 0);
    const minPrice = (open.length ? open : zones).reduce((m, z) => Math.min(m, z.price), Infinity);
    const avail = availability(e, left);
    const d = e.date_time ? new Date(e.date_time) : null;
    const ok = d && !Number.isNaN(d.getTime());
    const time = ok ? d.toLocaleTimeString("en-GB", { hour: "2-digit", minute: "2-digit" }) : "";
    const attrs = e.dynamic_attributes || {};
    const venue = e.venue || {};

    document.title = `${main}${sub ? " — " + sub : ""} | ENCORE`;
    $("#crumb-city").textContent = venue.city || "—";
    $("#crumb-name").textContent = main;

    // hero
    $("#event-tags").innerHTML =
      `<span class="badge badge--sale">${esc(e.category || "Concert")}</span>` +
      `<span class="badge badge--${avail.cls}">${esc(avail.label)}</span>`;
    $("#event-title").textContent = main;
    $("#event-sub").textContent = sub;
    $("#event-lead").textContent = e.artist?.bio || "One electric night. A whole new kind of afterglow.";
    $("#event-when").textContent = ok
      ? `${d.toLocaleDateString("en-GB", { day: "2-digit", month: "short", year: "numeric" })} · ${time}`
      : "Date to be announced";
    $("#event-where").textContent = [venue.name, venue.city].filter(Boolean).join(", ");

    // about
    $("#about-title").textContent = `A night with ${main}.`;
    $("#about-lead").textContent =
      `${main} brings ${sub || "a brand-new show"} to ${venue.name || "the stage"}${venue.city ? " in " + venue.city : ""}. ` +
      (e.artist?.genre ? `Expect a full ${e.artist.genre.toLowerCase()} set, ` : "Expect ") +
      "soaring moments and a crowd singing every word — from the first note to the last encore.";
    const stage = attrs.stage_specs || {};
    const fx = Array.isArray(stage.special_effects) ? stage.special_effects : [];
    $("#about-text").textContent = [
      stage.sound_system && `Sound by ${stage.sound_system}`,
      stage.lighting && `lighting by ${stage.lighting}`,
      fx.length && `with ${fx.join(", ")}`,
    ].filter(Boolean).join(", ").replace(/^./, (c) => c.toUpperCase()) + (stage.sound_system || stage.lighting || fx.length ? "." : "");

    // facts
    $("#fact-time").textContent = ok
      ? `${d.toLocaleDateString("en-GB", { weekday: "long" })} · Starts ${time}`
      : "Time to be announced";
    $("#fact-age").textContent = attrs.age_restriction ? `${attrs.age_restriction} · ID may be checked` : "All ages welcome";
    $("#fact-delivery").textContent = attrs.entry_gate ? `Secure mobile tickets · ${attrs.entry_gate}` : "Secure mobile tickets";

    // every other dynamic attribute becomes a "good to know" row (Mongo documents differ per event)
    const shown = new Set(["age_restriction", "entry_gate", "stage_specs"]);
    const rows = Object.entries(attrs).filter(([k, v]) => !shown.has(k) && v !== null && v !== "");
    const gtk = $("#good-to-know");
    gtk.innerHTML = rows.map(([k, v]) => `<div><dt>${esc(humanKey(k))}</dt><dd>${esc(humanValue(v))}</dd></div>`).join("");
    gtk.hidden = rows.length === 0;

    // venue
    $("#venue-name").textContent = [venue.name, venue.city].filter(Boolean).join(", ") || "Venue to be announced";
    $("#venue-text").textContent =
      [venue.address, venue.city, venue.country].filter(Boolean).join(", ") +
      (venue.capacity ? `. Capacity ${venue.capacity.toLocaleString("en-US")}.` : ".");
    const q = [venue.name, venue.address, venue.city, venue.country].filter(Boolean).join(", ");
    $("#directions").href = `https://www.google.com/maps/search/?api=1&query=${encodeURIComponent(q)}`;

    // ticket panel
    $("#price-from").textContent = Number.isFinite(minPrice) ? money(minPrice) : "TBA";
    const canBuy = avail.cls === "sale" || avail.cls === "fast";
    $("#zone-list").innerHTML = zones.map((z, i) => {
      const zl = seatsLeft(z);
      const out = zl === 0 || !canBuy;
      return `<label class="zone${out ? " is-out" : ""}">
          <input type="radio" name="zone" value="${i}" ${out ? "disabled" : ""} />
          <span class="zone__name">${esc(z.name)}<span class="zone__left">${zl === 0 ? "Sold out" : `${zl.toLocaleString("en-US")} left`}</span></span>
          <span class="zone__price">${money(z.price)}</span>
        </label>`;
    }).join("") || `<p class="muted-12">Zones will be announced soon.</p>`;
    $("#zone-list").querySelectorAll("input").forEach((input) =>
      input.addEventListener("change", () => { selectedZone = zones[Number(input.value)]; updateCta(); }));

    const leftEl = $("#seats-left");
    leftEl.className = "tickets__left" + (left === 0 || !canBuy ? " is-out" : left <= SELLING_FAST_SEATS ? " is-low" : "");
    leftEl.textContent = !canBuy
      ? `${avail.label} · Tickets aren’t available to buy`
      : `${left.toLocaleString("en-US")} seats available · Book your favourite view`;

    setSaved(readSaved().has(e.id));
    updateCta(canBuy);
  }

  function updateCta(canBuy = !$("#seats-left").classList.contains("is-out")) {
    // the seat map comes next: pass the event and (optionally) the chosen zone
    const btn = $("#seats-btn");
    if (!event || !canBuy) { btn.setAttribute("aria-disabled", "true"); btn.removeAttribute("href"); return; }
    const qs = new URLSearchParams({ id: event.id });
    if (selectedZone) qs.set("zone", selectedZone.name);
    btn.href = `../seats/seats.html?${qs}`;
    btn.setAttribute("aria-disabled", "false");
  }

  // ---------- save / share ----------
  function setSaved(on) {
    const b = $("#save-btn");
    b.setAttribute("aria-pressed", String(on));
    b.textContent = on ? "♥ Saved" : "♡ Save concert";
  }
  $("#save-btn").addEventListener("click", () => {
    if (!event) return;
    const saved = readSaved();
    const on = !saved.has(event.id);
    on ? saved.add(event.id) : saved.delete(event.id);
    writeSaved(saved);
    setSaved(on);
    toast(on ? "Saved to your list" : "Removed from your list");
  });
  $("#share-btn").addEventListener("click", async () => {
    const data = { title: document.title, url: location.href };
    try {
      if (navigator.share) { await navigator.share(data); return; }
      await navigator.clipboard.writeText(location.href);
      toast("Link copied");
    } catch (err) {
      if (err?.name !== "AbortError") toast("Copy the link from the address bar to share");
    }
  });

  // ---------- section tabs: highlight the section in view ----------
  const tabs = [...document.querySelectorAll(".tabs a")];
  const setTab = (id) => tabs.forEach((a) => a.classList.toggle("is-active", a.getAttribute("href") === `#${id}`));
  tabs.forEach((a) => a.addEventListener("click", () => setTab(a.getAttribute("href").slice(1))));
  if ("IntersectionObserver" in window) {
    const io = new IntersectionObserver((entries) => {
      entries.filter((en) => en.isIntersecting).forEach((en) => setTab(en.target.id));
    }, { rootMargin: "-110px 0px -60% 0px" });
    ["about", "info", "venue"].forEach((id) => io.observe(document.getElementById(id)));
  }

  $("#retry-btn").addEventListener("click", load);
  load();
})();
