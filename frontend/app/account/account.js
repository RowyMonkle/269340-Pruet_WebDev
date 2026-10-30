/* ENCORE fan dashboard: /account/account.html (log in required)
 *
 * GET /api/v1/orders/summary?user_id=&limit=&offset=  -> the fan's bookings, newest first (no tickets)
 * GET /api/v1/orders/{id}                             -> tickets of one booking (concert id, seat, code)
 * GET /api/v1/events/{id}                             -> concert name / date for each booking (cached)
 * GET /api/v1/users/{id}, PUT /api/v1/users/{id}      -> "Your details"
 */
(() => {
  "use strict";

  const API_BASE = "/api/v1";
  const CURRENCY = "฿";
  const PAGE = 20;              // bookings per request (the API allows up to 100)
  const PARALLEL = 6;           // order detail requests at the same time

  const $ = (s) => document.querySelector(s);
  const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
  const money = (n) => `${CURRENCY}${Number(n || 0).toLocaleString("en-US", { minimumFractionDigits: 2, maximumFractionDigits: 2 })}`;
  const asDate = (s) => (s ? new Date(/[zZ]|[+-]\d\d:\d\d$/.test(s) ? s : s + "Z") : null);   // API times are UTC
  const fmtDate = (d, withTime = true) => (d && !Number.isNaN(d.getTime())
    ? d.toLocaleDateString("en-GB", { day: "numeric", month: "short", year: "numeric" }) + (withTime ? ` · ${d.toLocaleTimeString("en-GB", { hour: "2-digit", minute: "2-digit" })}` : "")
    : "");

  const user = window.EncoreSession?.get();
  if (!user) { location.replace(window.EncoreSession.loginHref("/account/account.html")); return; }

  const state = { orders: [], details: new Map(), events: new Map(), eventData: new Map(), offset: 0, done: false, filter: "all" };

  async function getJSON(path) {
    const res = await fetch(`${API_BASE}${path}`, { headers: { Accept: "application/json" } });
    if (!res.ok) { const err = new Error(`HTTP ${res.status}`); err.status = res.status; throw err; }
    return res.json();
  }
  // run fn over items, at most `limit` at a time (do not flood the API with 20 requests at once)
  async function pool(items, limit, fn) {
    const queue = [...items];
    await Promise.all(Array.from({ length: Math.min(limit, queue.length) }, async () => {
      while (queue.length) await fn(queue.shift()).catch(() => null);
    }));
  }
  function eventOf(id) {
    if (!state.events.has(id)) {   // one request per concert, shared by all bookings of that concert
      state.events.set(id, getJSON(`/events/${encodeURIComponent(id)}`)
        .then((e) => { state.eventData.set(id, e); return e; })
        .catch(() => null));
    }
    return state.events.get(id);
  }

  // pending orders whose 10 minutes ran out are shown as expired even before the sweeper flips them
  function statusOf(o) {
    if (o.status === "pending" && o.expires_at && asDate(o.expires_at) < new Date()) return "expired";
    return o.status === "paid" ? "confirmed" : o.status;
  }
  const BADGE = {
    confirmed: ["sale", "Paid"], pending: ["fast", "Waiting for payment"],
    expired: ["out", "Expired"], cancelled: ["out", "Cancelled"],
  };

  // ---------- load ----------
  async function loadMore() {
    $("#more-btn").disabled = true;
    try {
      const page = await getJSON(`/orders/summary?user_id=${user.id}&limit=${PAGE}&offset=${state.offset}`);
      state.offset += page.length;
      state.done = page.length < PAGE;
      state.orders.push(...page);
      render();
      // details (concert + seats) for the new rows, a few at a time
      await pool(page, PARALLEL, async (o) => {
        const d = await getJSON(`/orders/${o.id}`);
        state.details.set(o.id, d);
        const evId = d.tickets?.[0]?.event_id;
        if (evId) await eventOf(evId);
        renderRow(o);
      });
      renderStats();
    } catch (err) {
      if (err.status === 404) {   // the stored account no longer exists
        window.EncoreSession.clear();
        location.replace(window.EncoreSession.loginHref("/account/account.html"));
        return;
      }
      $("#order-empty").innerHTML = "We couldn’t load your bookings right now. Please refresh in a moment.";
      $("#order-empty").hidden = false;
    } finally {
      $("#more-btn").disabled = false;
      $("#more-btn").hidden = state.done;
      $("#acct").removeAttribute("aria-busy");
    }
  }

  // ---------- render ----------
  function visible(o) {
    const s = statusOf(o);
    if (state.filter === "all") return true;
    if (state.filter === "past") return s === "expired" || s === "cancelled";
    return s === state.filter;
  }
  function render() {
    const list = $("#order-list");
    list.innerHTML = state.orders.map((o) => `<li class="order" id="order-${o.id}" data-id="${o.id}"></li>`).join("");
    state.orders.forEach(renderRow);
    renderStats();
  }
  function renderRow(o) {
    const li = document.getElementById(`order-${o.id}`);
    if (!li) return;
    li.hidden = !visible(o);
    const d = state.details.get(o.id);
    const event = d?.tickets?.[0] ? state.eventData.get(d.tickets[0].event_id) : null;
    {
      const s = statusOf(o);
      const [cls, label] = BADGE[s] || ["plum", s];
      const title = event ? (event.artist?.name && !event.title.includes(event.artist.name) ? `${event.artist.name} · ${event.title}` : event.title) : (d ? "Concert" : "Loading…");
      const when = event?.date_time ? fmtDate(asDate(event.date_time)) : "";
      const open = li.classList.contains("is-open");
      li.innerHTML = `
        <button type="button" class="order__row" aria-expanded="${open}" aria-controls="order-body-${o.id}">
          <span class="order__main">
            <span class="order__name">${esc(title)}</span>
            <span class="order__meta">${esc(o.order_number)} · ${o.ticket_count} ticket${o.ticket_count === 1 ? "" : "s"}${when ? ` · ${esc(when)}` : ""}${event?.venue?.name ? ` · ${esc(event.venue.name)}` : ""}</span>
          </span>
          <span class="badge badge--${cls}">${esc(label)}</span>
          <span class="order__amount">${money(o.total_amount)} <img class="order__chev" src="../assets/chevron-down.svg" width="15" height="15" alt="" /></span>
        </button>
        <div class="order__body" id="order-body-${o.id}" ${open ? "" : "hidden"}>${body(o, d, s, event)}</div>`;
      if (open) li.classList.add("is-open");
    }
  }
  function body(o, d, s, event) {
    if (!d) return `<div class="skel-line"></div>`;
    const tickets = (d.tickets || []).map((t) => {
      const voided = t.status !== "valid";
      return `<div class="ticket${voided ? " is-void" : ""}">
          <b>${esc(t.seat_zone)}${t.seat_number ? ` · ${esc(t.seat_number)}` : ""}</b>
          <small>${esc({ valid: "Valid ticket", held: "Held, not paid yet", cancelled: "Released" }[t.status] || t.status)} · ${money(t.price)}</small>
          <code>${s === "confirmed" ? esc(t.ticket_code) : "Code appears after payment"}</code>
        </div>`;
    }).join("");
    let actions = "";
    if (s === "pending") {
      const left = Math.max(0, Math.round((asDate(o.expires_at) - Date.now()) / 60000));
      actions = `<a class="btn btn--primary" href="../checkout/checkout.html?order=${o.id}">Complete payment</a><span class="order__hint">Seats held for about ${left} more minute${left === 1 ? "" : "s"}.</span>`;
    } else if (s === "expired" || s === "cancelled") {
      actions = event ? `<a class="btn btn--outline" href="../seats/seats.html?id=${encodeURIComponent(event.id)}">Choose seats again</a><span class="order__hint">Nothing was charged.</span>` : `<span class="order__hint">Nothing was charged.</span>`;
    } else {
      const paid = (d.payments || []).find((p) => p.status === "completed");
      actions = `<span class="order__hint">Paid ${money(o.paid_amount)}${paid ? ` by ${esc(paid.provider.replace("_", " "))} · ${esc(fmtDate(asDate(paid.created_at)))}` : ""}</span>
        ${event ? `<a class="btn btn--outline" href="../event/event.html?id=${encodeURIComponent(event.id)}">Concert details</a>` : ""}`;
    }
    return `<div class="tickets">${tickets}</div><div class="order__actions">${actions}</div>`;
  }

  function renderStats() {
    const now = new Date();
    let tickets = 0, paid = 0, pending = 0, spent = 0;
    for (const o of state.orders) {
      const s = statusOf(o);
      if (s === "confirmed") {
        paid++; spent += Number(o.paid_amount || o.total_amount);
        const d = state.details.get(o.id);
        const ev = d?.tickets?.[0] ? state.eventData.get(d.tickets[0].event_id) : null;
        // count tickets of concerts that have not happened yet (unknown date counts as upcoming)
        const evDate = ev?.date_time ? asDate(ev.date_time) : null;
        if (!evDate || evDate > now) tickets += o.ticket_count;
      } else if (s === "pending") pending++;
    }
    $("#st-tickets").textContent = tickets;
    $("#st-paid").textContent = paid;
    $("#st-pending").textContent = pending;
    $("#st-spent").textContent = money(spent);
    const shown = state.orders.filter(visible).length;
    const empty = $("#order-empty");
    if (!state.orders.length) {
      empty.innerHTML = `No bookings yet. <a href="../#concerts">Find your first night →</a>`;
      empty.hidden = false;
    } else if (!shown) {
      empty.textContent = "No bookings in this list.";
      empty.hidden = false;
    } else empty.hidden = true;
  }

  // ---------- events ----------
  $("#order-list").addEventListener("click", (e) => {
    const row = e.target.closest(".order__row");
    if (!row) return;
    const li = row.closest(".order");
    const open = li.classList.toggle("is-open");
    row.setAttribute("aria-expanded", String(open));
    li.querySelector(".order__body").hidden = !open;
  });
  $("#filters").addEventListener("click", (e) => {
    const chip = e.target.closest("[data-filter]");
    if (!chip) return;
    state.filter = chip.dataset.filter;
    document.querySelectorAll("#filters .chip").forEach((c) => {
      const on = c === chip;
      c.classList.toggle("is-active", on);
      c.setAttribute("aria-pressed", String(on));
    });
    state.orders.forEach((o) => { const li = document.getElementById(`order-${o.id}`); if (li) li.hidden = !visible(o); });
    renderStats();
  });
  $("#more-btn").addEventListener("click", loadMore);

  // ---------- profile ----------
  function paintProfile(u) {
    $("#p-first").value = u.first_name || (u.full_name || "").split(" ")[0] || "";
    $("#p-last").value = u.last_name ?? (u.full_name || "").split(" ").slice(1).join(" ");
    $("#p-email").value = u.email || "";
    const initials = (($("#p-first").value[0] || "") + ($("#p-last").value[0] || "")).toUpperCase() || "?";
    $("#avatar").textContent = initials;
    $("#hello").textContent = `Hi, ${$("#p-first").value || "there"}.`;
    $("#p-meta").textContent = `Username ${u.username || "—"} · member since ${fmtDate(asDate(u.created_at), false) || "—"}`;
  }
  getJSON(`/users/${user.id}`).then(paintProfile).catch(() => paintProfile(user));

  $("#profile-form").addEventListener("submit", async (e) => {
    e.preventDefault();
    const msg = $("#p-msg");
    const first = $("#p-first").value.trim();
    const email = $("#p-email").value.trim();
    const badFirst = !first, badEmail = !/^[^\s@]+@[^\s@]+\.[^\s@]+$/.test(email);
    $("#p-first").setAttribute("aria-invalid", String(badFirst));
    $("#p-email").setAttribute("aria-invalid", String(badEmail));
    if (badFirst || badEmail) {
      msg.className = "pmsg is-error"; msg.hidden = false;
      msg.textContent = badFirst ? "Enter your first name." : "Enter a valid email address.";
      return;
    }
    $("#p-save").disabled = true;
    try {
      const res = await fetch(`${API_BASE}/users/${user.id}`, {
        method: "PUT",
        headers: { "Content-Type": "application/json", Accept: "application/json" },
        body: JSON.stringify({ first_name: first, last_name: $("#p-last").value.trim(), email }),
      });
      const body = await res.json().catch(() => ({}));
      msg.hidden = false;
      if (res.ok) {
        paintProfile(body);
        // keep the nav greeting in step: store the updated public record the same way the login did
        let remembered = false;
        try { remembered = Boolean(localStorage.getItem("encore.user")); } catch { /* ignore */ }
        window.EncoreSession.set({ ...user, ...body }, remembered);
        const hi = document.querySelector(".nav__hello");
        if (hi) hi.textContent = `Hi, ${body.first_name || first}`;
        msg.className = "pmsg"; msg.textContent = "Saved.";
      } else {
        msg.className = "pmsg is-error";
        msg.textContent = res.status === 409 ? "That email is already used by another account." : "We couldn’t save your details. Please try again.";
      }
    } catch {
      msg.className = "pmsg is-error"; msg.hidden = false; msg.textContent = "We couldn’t reach ENCORE. Please try again.";
    } finally {
      $("#p-save").disabled = false;
    }
  });

  loadMore();
})();
