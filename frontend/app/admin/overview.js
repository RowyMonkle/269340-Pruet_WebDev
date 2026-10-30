/* Backstage overview: numbers computed from the public read APIs.
 *   GET /events (+ /events/{id} for zones) -> booked value, seats, live concerts, top concerts, low inventory
 *   GET /users?page_size=1                  -> registered fans (total)
 *   GET /users?page=<last>                  -> newest fans (users are listed oldest first)
 */
(() => {
  "use strict";
  const A = window.Admin;
  if (!A) return;   // gate is showing
  const $ = (s) => document.querySelector(s);
  const LOW_LEFT = 0.1;   // zone with < 10% seats left -> attention

  const h = new Date().getHours();
  $("#greeting").textContent = `${h < 12 ? "Good morning" : h < 18 ? "Good afternoon" : "Good evening"}, ${A.me.first_name || "there"}.`;

  let events = [];

  async function load(fresh = false) {
    $("#refresh-btn").disabled = true;
    $("#export-btn").disabled = true;
    loadFans();
    try {
      events = await A.loadEvents({ fresh, onProgress: (d, t) => { $("#load-note") && ($("#load-note").textContent = `Loading concerts… ${d} / ${t}`); } });
      renderMetrics();
      renderChart();
      renderTop();
      renderAttention();
      $("#export-btn").disabled = false;
    } catch (err) {
      $("#chart").innerHTML = `<p class="note">Couldn’t load concerts (${A.esc(err.message)}).</p>`;
    } finally {
      $("#refresh-btn").disabled = false;
      $("#content").removeAttribute("aria-busy");
    }
  }

  function renderMetrics() {
    const now = new Date();
    const value = events.reduce((n, e) => n + e.value, 0);
    const booked = events.reduce((n, e) => n + e.booked, 0);
    const capacity = events.reduce((n, e) => n + e.capacity, 0);
    const upcoming = events.filter((e) => A.asDate(e.date_time) > now && !/cancel|complete/i.test(e.status));
    const soldOut = upcoming.filter((e) => e.capacity && e.booked >= e.capacity).length;
    $("#m-value").textContent = A.money(value);
    $("#m-value-sub").textContent = `across ${A.num(events.length)} concerts`;
    $("#m-booked").textContent = A.num(booked);
    $("#m-booked-sub").textContent = `${capacity ? ((booked / capacity) * 100).toFixed(1) : 0}% of ${A.num(capacity)} seats`;
    $("#m-live").textContent = A.num(upcoming.length);
    $("#m-live-sub").textContent = soldOut ? `${soldOut} sold out` : "none sold out";
  }

  async function loadFans() {
    try {
      const first = await A.getJSON(`/users?page=1&page_size=1`);
      $("#m-fans").textContent = A.num(first.total);
      const perPage = 5;
      const last = Math.max(1, Math.ceil(first.total / perPage));
      let newest = (await A.getJSON(`/users?page=${last}&page_size=${perPage}`)).items;
      if (newest.length < perPage && last > 1) newest = (await A.getJSON(`/users?page=${last - 1}&page_size=${perPage}`)).items.concat(newest);
      newest = newest.sort((a, b) => b.id - a.id).slice(0, perPage);
      const month = new Date(); month.setDate(1); month.setHours(0, 0, 0, 0);
      $("#m-fans-sub").textContent = `newest joined ${A.fmtDate(A.asDate(newest[0]?.created_at))}`;
      $("#fans-body").innerHTML = newest.map((u) => `
        <tr>
          <td class="cell-2"><b>${A.esc([u.first_name, u.last_name].filter(Boolean).join(" ") || u.full_name || "—")}</b><small>${A.esc(u.email)}</small></td>
          <td>${A.esc(u.username)}</td>
          <td><span class="badge ${u.role === "admin" ? "badge--fast" : "badge--plum"}">${A.esc(u.role)}</span></td>
          <td>${A.fmtDate(A.asDate(u.created_at), true)}</td>
        </tr>`).join("");
    } catch {
      $("#m-fans").textContent = "—";
      $("#fans-body").innerHTML = `<tr><td colspan="4" class="note">Couldn’t load users.</td></tr>`;
    }
  }

  function renderChart() {
    // 12 buckets starting this month
    const start = new Date(); start.setDate(1); start.setHours(0, 0, 0, 0);
    const buckets = Array.from({ length: 12 }, (_, i) => ({ d: new Date(start.getFullYear(), start.getMonth() + i, 1), v: 0 }));
    for (const e of events) {
      const d = A.asDate(e.date_time);
      if (!d) continue;
      const i = (d.getFullYear() - start.getFullYear()) * 12 + d.getMonth() - start.getMonth();
      if (i >= 0 && i < 12) buckets[i].v += e.value;
    }
    const max = Math.max(1, ...buckets.map((b) => b.v));
    const short = (v) => (v >= 1e6 ? `${(v / 1e6).toFixed(1)}M` : v >= 1e3 ? `${Math.round(v / 1e3)}k` : String(Math.round(v)));
    $("#chart").innerHTML = `
      <div class="bars" role="img" aria-label="Booked value per month for the next 12 months">
        ${buckets.map((b) => `<div class="bars__col" title="${b.d.toLocaleDateString("en-GB", { month: "long", year: "numeric" })}: ${A.money(b.v)}">
          <span class="bars__val">${b.v ? "฿" + short(b.v) : ""}</span>
          <span class="bars__bar" style="height:${(b.v / max) * 85}%"></span></div>`).join("")}
      </div>
      <div class="bars__axis">${buckets.map((b) => `<span>${b.d.toLocaleDateString("en-GB", { month: "short" })}</span>`).join("")}</div>`;
  }

  function renderTop() {
    const top = [...events].sort((a, b) => b.value - a.value).slice(0, 5);
    $("#top-list").innerHTML = top.map((e) => `
      <li class="prog" style="width:100%">
        <span class="prog__top"><b title="${A.esc(A.title(e))}">${A.esc(e.artist?.name || e.title)}</b><span>${A.money(e.value)}</span></span>
        <span class="prog__track"><span class="prog__fill${e.soldPct >= 100 ? " is-full" : ""}" style="width:${Math.min(100, e.soldPct)}%"></span></span>
        <small>${e.soldPct.toFixed(1)}% booked · ${A.esc(e.venue?.city || "")}</small>
      </li>`).join("") || `<li class="note">No concerts yet.</li>`;
  }

  function renderAttention() {
    const now = new Date();
    const items = [];
    for (const e of events) {
      if (A.asDate(e.date_time) < now) continue;
      for (const z of e.zones || []) {
        const left = z.capacity - z.booked_count;
        if (z.capacity && left <= 0) items.push([0, "SOLD OUT", `${A.title(e)} · ${z.name}`]);
        else if (z.capacity && left / z.capacity < LOW_LEFT) items.push([1, "LOW", `${A.title(e)} · ${z.name} — ${left} left`]);
      }
    }
    const soon = events.filter((e) => { const d = A.asDate(e.date_time); return d > now && d - now < 7 * 864e5; });
    soon.forEach((e) => items.push([2, "THIS WEEK", `${A.title(e)} · ${A.fmtDate(A.asDate(e.date_time), true)}`]));
    items.sort((a, b) => a[0] - b[0]);
    $("#attention").innerHTML = items.slice(0, 8).map(([, tag, text]) => `<li><span class="tag">${tag}</span><span>${A.esc(text)}</span></li>`).join("")
      || `<li class="note">Nothing needs attention: no zone is under 10% left and no concert is this week.</li>`;
  }

  $("#refresh-btn").addEventListener("click", () => load(true));
  $("#export-btn").addEventListener("click", () => {
    A.downloadCSV(`encore-concerts-${new Date().toISOString().slice(0, 10)}.csv`, [
      ["Concert", "Artist", "Date", "Venue", "City", "Status", "Capacity", "Seats booked", "% booked", "Booked value (THB)"],
      ...[...events].sort((a, b) => b.value - a.value).map((e) => [e.title, e.artist?.name, e.date_time, e.venue?.name, e.venue?.city, e.status, e.capacity, e.booked, e.soldPct.toFixed(1), e.value]),
    ]);
  });

  load();
})();
