/* Backstage · Concerts & seating
 *   GET  /events + /events/{id}   -> table with capacity / booked per concert, zone breakdown in a drawer
 *   POST /events                  -> "Add concert" (artist, venue, date, zones)
 */
(() => {
  "use strict";
  const A = window.Admin;
  if (!A) return;
  const $ = (s) => document.querySelector(s);
  const PER_PAGE = 20;
  const LOW = 90;   // % booked counted as "almost sold out"

  let events = [];
  const view = { f: "upcoming", q: "", page: 0 };

  async function load(fresh = false) {
    $("#refresh-btn").disabled = true;
    try {
      events = await A.loadEvents({ fresh, onProgress: (d, t) => { const n = $("#load-note"); if (n) n.textContent = `Loading concerts… ${d} / ${t}`; } });
      render();
    } catch (err) {
      $("#rows").innerHTML = `<tr><td colspan="8" class="note">Couldn’t load concerts (${A.esc(err.message)}).</td></tr>`;
    } finally {
      $("#refresh-btn").disabled = false;
      $("#content").removeAttribute("aria-busy");
    }
  }

  function filtered() {
    const now = new Date();
    const q = view.q.toLowerCase();
    return events.filter((e) => {
      const d = A.asDate(e.date_time);
      if (view.f === "upcoming" && !(d > now)) return false;
      if (view.f === "past" && !(d <= now)) return false;
      if (view.f === "low" && !(d > now && e.soldPct >= LOW)) return false;
      if (!q) return true;
      return [e.title, e.artist?.name, e.venue?.name, e.venue?.city, e.category].some((v) => String(v || "").toLowerCase().includes(q));
    });
  }

  function render() {
    const list = filtered();
    const pages = Math.max(1, Math.ceil(list.length / PER_PAGE));
    view.page = Math.min(view.page, pages - 1);
    const slice = list.slice(view.page * PER_PAGE, (view.page + 1) * PER_PAGE);
    $("#rows").innerHTML = slice.map((e) => `
      <tr class="is-click" data-id="${A.esc(e.id)}" tabindex="0">
        <td class="cell-2"><b>${A.esc(e.artist?.name || "—")}</b><small>${A.esc(e.title)}</small></td>
        <td>${A.fmtDate(A.asDate(e.date_time), true)}</td>
        <td class="cell-2"><b>${A.esc(e.venue?.name || "—")}</b><small>${A.esc(e.venue?.city || "")}</small></td>
        <td><span class="badge ${/cancel/i.test(e.status) ? "badge--out" : e.soldPct >= 100 ? "badge--out" : e.soldPct >= LOW ? "badge--fast" : "badge--sale"}">${A.esc(e.soldPct >= 100 ? "sold out" : e.status)}</span></td>
        <td class="num">${(e.zones || []).length}</td>
        <td class="num">${A.num(e.booked)} / ${A.num(e.capacity)}</td>
        <td><span class="prog"><span class="prog__track"><span class="prog__fill${e.soldPct >= 100 ? " is-full" : ""}" style="width:${Math.min(100, e.soldPct)}%"></span></span><small>${e.soldPct.toFixed(1)}%</small></span></td>
        <td class="num">${A.money(e.value)}</td>
      </tr>`).join("") || `<tr><td colspan="8" class="note">No concerts match.</td></tr>`;
    $("#count").textContent = `${A.num(list.length)} concert${list.length === 1 ? "" : "s"} · page ${view.page + 1} of ${pages}`;
    $("#prev").disabled = view.page === 0;
    $("#next").disabled = view.page >= pages - 1;
    $("#sub").textContent = `${A.num(events.length)} concerts · ${A.num(events.reduce((n, e) => n + e.booked, 0))} of ${A.num(events.reduce((n, e) => n + e.capacity, 0))} seats booked.`;
  }

  // ---------- concert drawer ----------
  function openConcert(id) {
    const e = events.find((x) => x.id === id);
    if (!e) return;
    const zones = [...(e.zones || [])].sort((a, b) => b.price - a.price);
    const attrs = Object.entries(e.dynamic_attributes || {});
    const show = (v) => (typeof v === "boolean" ? (v ? "Yes" : "No") : Array.isArray(v) ? v.join(", ") : v && typeof v === "object" ? Object.entries(v).map(([k, x]) => `${k.replace(/_/g, " ")}: ${show(x)}`).join(" · ") : String(v));
    A.openDrawer(
      `<h2>${A.esc(A.title(e))}</h2><p>${A.fmtDate(A.asDate(e.date_time), true)} · ${A.esc([e.venue?.name, e.venue?.city, e.venue?.country].filter(Boolean).join(", "))}</p>`,
      `<div class="stack">
         <p class="note">ID <code>${A.esc(e.id)}</code> · ${A.esc(e.category)} · status ${A.esc(e.status)} · venue capacity ${A.num(e.venue?.capacity)}</p>
         ${zones.map((z) => {
           const pct = z.capacity ? (z.booked_count / z.capacity) * 100 : 0;
           return `<div class="prog">
             <span class="prog__top"><b>${A.esc(z.name)}</b><span>${A.money(z.price)} · ${A.num(z.booked_count)} / ${A.num(z.capacity)}</span></span>
             <span class="prog__track"><span class="prog__fill${pct >= 100 ? " is-full" : ""}" style="width:${Math.min(100, pct)}%"></span></span>
             <small>${pct.toFixed(1)}% booked · ${A.num(Math.max(0, z.capacity - z.booked_count))} left · ${A.money(z.booked_count * z.price)}</small>
           </div>`;
         }).join("")}
         ${window.EncoreArena?.fits(e.zones) ? `<div class="venue-plan" id="drawer-plan"></div>` : ""}
         <p class="note"><b>Total:</b> ${A.num(e.booked)} of ${A.num(e.capacity)} seats (${e.soldPct.toFixed(1)}%) · ${A.money(e.value)}</p>
         ${attrs.length ? `<table class="table"><tbody>${attrs.map(([k, v]) => `<tr><td>${A.esc(k.replace(/_/g, " "))}</td><td>${A.esc(show(v))}</td></tr>`).join("")}</tbody></table>` : ""}
         ${(e.tags || []).length ? `<p class="note">Tags: ${e.tags.map(A.esc).join(", ")}</p>` : ""}
         <a class="btn btn--outline" href="../event/event.html?id=${encodeURIComponent(e.id)}" target="_blank" rel="noopener">Open public page ↗</a>
       </div>`);
    const plan = document.getElementById("drawer-plan");
    if (plan) window.EncoreArena.render(plan, e.zones, {});   // same plan the fans see, sold-out zones greyed
  }

  // ---------- add concert ----------
  function zoneRow(z = {}) {
    return `<div class="zone-row">
      <input class="finput" name="zname" placeholder="Zone name" value="${A.esc(z.name || "")}" maxlength="50" />
      <input class="finput" name="zprice" type="number" min="0" step="1" placeholder="Price ฿" value="${z.price ?? ""}" />
      <input class="finput" name="zcap" type="number" min="1" step="1" placeholder="Seats" value="${z.capacity ?? ""}" />
      <button type="button" class="icon-x" data-remove-zone aria-label="Remove zone">×</button>
    </div>`;
  }
  function openAdd() {
    const body = A.openDrawer(`<h2>Add concert</h2><p>Creates the event in MongoDB (POST /events). It goes on sale straight away.</p>`, `
      <form id="add-form" class="stack" novalidate>
        <div class="fgrid">
          <label class="fld full"><span>Title</span><input class="finput" name="title" required minlength="3" maxlength="200" placeholder="Afterglow Tour" /></label>
          <label class="fld"><span>Artist</span><input class="finput" name="artist" required maxlength="100" /></label>
          <label class="fld"><span>Genre</span><input class="finput" name="genre" required maxlength="50" placeholder="Pop" /></label>
          <label class="fld"><span>Category</span><select class="finput" name="category"><option>Concert</option><option>Music Festival</option><option>Live Performance</option></select></label>
          <label class="fld"><span>Date &amp; time</span><input class="finput" name="date" type="datetime-local" required /></label>
          <label class="fld"><span>Venue</span><input class="finput" name="venue" required maxlength="100" /></label>
          <label class="fld"><span>City</span><input class="finput" name="city" required maxlength="100" value="Bangkok" /></label>
          <label class="fld"><span>Country</span><input class="finput" name="country" maxlength="100" value="Thailand" /></label>
          <label class="fld"><span>Venue capacity</span><input class="finput" name="vcap" type="number" min="1" required /></label>
          <label class="fld"><span>Age guidance (optional)</span><input class="finput" name="age" placeholder="16+" maxlength="20" /></label>
        </div>
        <div class="fld"><span>Zones</span><div class="zone-rows" id="zone-rows">${zoneRow({ name: "VIP", price: 4500, capacity: 200 })}${zoneRow({ name: "General Admission", price: 1500, capacity: 1000 })}</div></div>
        <button type="button" class="btn btn--outline" id="add-zone">+ Add zone</button>
        <p class="msg is-error" id="add-msg" role="alert" hidden></p>
        <button type="submit" class="btn btn--primary" id="add-save">Create concert</button>
      </form>`);
    const form = body.querySelector("#add-form");
    body.querySelector("#add-zone").addEventListener("click", () => body.querySelector("#zone-rows").insertAdjacentHTML("beforeend", zoneRow()));
    body.querySelector("#zone-rows").addEventListener("click", (e) => { if (e.target.closest("[data-remove-zone]")) e.target.closest(".zone-row").remove(); });
    form.addEventListener("submit", (e) => { e.preventDefault(); create(form); });
    form.querySelector('[name="title"]').focus();
  }

  async function create(form) {
    const msg = form.querySelector("#add-msg");
    const v = (n) => form.querySelector(`[name="${n}"]`).value.trim();
    const bad = [];
    const need = (n, ok) => { form.querySelector(`[name="${n}"]`).setAttribute("aria-invalid", String(!ok)); if (!ok) bad.push(n); };
    need("title", v("title").length >= 3); need("artist", !!v("artist")); need("genre", !!v("genre"));
    need("date", !!v("date")); need("venue", !!v("venue")); need("city", !!v("city")); need("vcap", Number(v("vcap")) > 0);
    const zones = [...form.querySelectorAll(".zone-row")].map((r) => ({
      name: r.querySelector('[name="zname"]').value.trim(),
      price: Number(r.querySelector('[name="zprice"]').value),
      capacity: Number(r.querySelector('[name="zcap"]').value),
      booked_count: 0,
    }));
    const zonesOk = zones.length && zones.every((z) => z.name && z.price >= 0 && Number.isFinite(z.price) && z.capacity > 0);
    const totalZones = zones.reduce((n, z) => n + (z.capacity || 0), 0);
    if (bad.length || !zonesOk) {
      msg.hidden = false;
      msg.textContent = !zonesOk ? "Every zone needs a name, a price of 0 or more and at least 1 seat." : "Fill in the highlighted fields.";
      return;
    }
    if (totalZones > Number(v("vcap"))) {
      msg.hidden = false;
      msg.textContent = `Zones add up to ${A.num(totalZones)} seats, more than the venue capacity (${A.num(v("vcap"))}).`;
      return;
    }
    const payload = {
      title: v("title"),
      category: v("category"),
      artist: { name: v("artist"), genre: v("genre") },
      venue: { name: v("venue"), city: v("city"), country: v("country") || "Thailand", capacity: Number(v("vcap")) },
      date_time: new Date(v("date")).toISOString(),   // datetime-local is local time -> send UTC
      zones,
      tags: [v("genre").toLowerCase(), v("city").toLowerCase()].filter(Boolean),
      dynamic_attributes: v("age") ? { age_restriction: v("age") } : {},
    };
    const btn = form.querySelector("#add-save");
    btn.disabled = true;
    try {
      const created = await A.getJSON("/events", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(payload) });
      A.closeDrawer();
      events = await A.loadEvents({ fresh: true });
      view.f = "all"; view.q = created.artist?.name || ""; $("#q").value = view.q;
      setChip("all");
      render();
      openConcert(created.id);
    } catch (err) {
      msg.hidden = false;
      msg.textContent = err.status === 400 ? `The API rejected it: ${typeof err.body?.detail === "string" ? err.body.detail : "check the fields"}.` : `Couldn’t create the concert (${err.message}).`;
    } finally {
      btn.disabled = false;
    }
  }

  // ---------- events ----------
  function setChip(f) {
    document.querySelectorAll("#filters .chip").forEach((c) => { const on = c.dataset.f === f; c.classList.toggle("is-active", on); c.setAttribute("aria-pressed", String(on)); });
  }
  $("#filters").addEventListener("click", (e) => { const c = e.target.closest("[data-f]"); if (!c) return; view.f = c.dataset.f; view.page = 0; setChip(view.f); render(); });
  $("#q").addEventListener("input", (e) => { view.q = e.target.value.trim(); view.page = 0; render(); });
  $("#prev").addEventListener("click", () => { view.page--; render(); });
  $("#next").addEventListener("click", () => { view.page++; render(); });
  $("#rows").addEventListener("click", (e) => { const tr = e.target.closest("tr[data-id]"); if (tr) openConcert(tr.dataset.id); });
  $("#rows").addEventListener("keydown", (e) => { const tr = e.target.closest("tr[data-id]"); if (tr && e.key === "Enter") openConcert(tr.dataset.id); });
  $("#refresh-btn").addEventListener("click", () => load(true));
  $("#add-btn").addEventListener("click", openAdd);

  load();
})();
