/* ENCORE checkout & payment: /checkout/checkout.html?order=<order id>
 *
 * 1. GET /api/v1/orders/{id}           -> tickets, total, status, expires_at (the 10-minute seat hold)
 * 2. GET /api/v1/events/{event_id}     -> concert name, date, venue for the summary
 *    GET /api/v1/users/{user_id}       -> prefill the "Your details" form
 * 3. Pay -> POST /api/v1/orders/{id}/payments { amount, provider, provider_tx_id }
 *    201 -> order "confirmed", tickets "valid" -> show the confirmation (step 3)
 *
 * TEST MODE: there is no real payment gateway. Card fields are validated in the browser only
 * and are NEVER sent to the API or stored; the API receives the provider name and a demo
 * transaction id, nothing else.
 */
(() => {
  "use strict";

  const API_BASE = "/api/v1";
  const CURRENCY = "฿";
  const DETAILS_KEY = "encore.details";   // localStorage: name/email/country/postcode when "Save my details" is ticked

  const $ = (sel) => document.querySelector(sel);
  const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
  const money = (n) => `${CURRENCY}${Number(n).toLocaleString("en-US", { minimumFractionDigits: 2, maximumFractionDigits: 2 })}`;
  const plural = (n, w) => `${n} ${w}${n === 1 ? "" : "s"}`;
  const orderId = new URLSearchParams(location.search).get("order");

  let order = null;
  let event = null;
  let timer = null;

  // ---------- helpers ----------
  async function getJSON(path) {
    const res = await fetch(`${API_BASE}${path}`, { headers: { Accept: "application/json" } });
    const body = await res.json().catch(() => ({}));
    return { ok: res.ok, status: res.status, body, res };
  }
  function readJSON(key) { try { return JSON.parse(localStorage.getItem(key)); } catch { return null; } }
  function writeJSON(key, v) { try { localStorage.setItem(key, JSON.stringify(v)); } catch { /* ignore */ } }
  function splitTitle(e) {
    const artist = e?.artist?.name;
    const parts = String(e?.title || "").split(/\s+[—–-]\s+/);
    if (parts.length > 1) return { main: parts[0], sub: parts.slice(1).join(" — ") };
    if (artist && e.title && artist !== e.title) return { main: artist, sub: e.title };
    return { main: e?.title || artist || "Your concert", sub: "" };
  }

  // ---------- load ----------
  async function load() {
    $("#checkout-state").hidden = true;
    if (!/^\d+$/.test(orderId || "")) return showState("missing");
    try {
      const o = await getJSON(`/orders/${orderId}`);
      if (!o.ok) return showState(o.status >= 500 ? "busy" : "missing");
      order = o.body;
      const firstEvent = order.tickets?.[0]?.event_id;
      // the event and the user are only for display: the page still works if either call fails
      const [ev, user] = await Promise.all([
        firstEvent ? getJSON(`/events/${encodeURIComponent(firstEvent)}`).catch(() => null) : null,
        getJSON(`/users/${order.user_id}`).catch(() => null),
      ]);
      event = ev?.ok ? ev.body : null;
      renderSummary();
      if (order.status === "confirmed" || order.status === "paid") return showDone();
      if (order.status === "expired" || order.status === "cancelled") return showState(order.status);
      prefill(user?.ok ? user.body : null);
      startCountdown();
      $("#pay-btn").disabled = false;
    } catch {
      showState("busy");
    } finally {
      $("#booking").removeAttribute("aria-busy");
    }
  }

  function showState(kind) {
    const back = event ? `../seats/seats.html?id=${encodeURIComponent(event.id)}` : "../#concerts";
    const copy = {
      missing:   ["We couldn’t find that order.", "The link may be old, or the order belongs to another account.", "Browse concerts", "../#concerts"],
      busy:      ["We can’t load your order right now.", "The ticket service is busy. Please try again in a moment.", "Browse concerts", "../#concerts"],
      expired:   ["Your seat hold has ended.", "Seats are held for 10 minutes. They’ve gone back on sale — pick them again to book.", "Choose seats again", back],
      cancelled: ["This order was cancelled.", "Nothing was charged. You can pick new seats any time.", "Choose seats again", back],
    }[kind];
    $("#state-title").textContent = copy[0];
    $("#state-text").textContent = copy[1];
    $("#state-cta").textContent = copy[2];
    $("#state-cta").href = copy[3];
    $("#retry-btn").hidden = kind !== "busy";
    $("#checkout-state").hidden = false;
    $("#workspace").hidden = true;
    $("#hold-badge").hidden = true;
    $("#pay-btn").disabled = true;
    clearInterval(timer);
  }

  // ---------- summary ----------
  function renderSummary() {
    const tickets = order.tickets || [];
    const { main, sub } = splitTitle(event);
    const d = event?.date_time ? new Date(event.date_time) : null;
    const ok = d && !Number.isNaN(d.getTime());
    $("#sum-name").textContent = [main, sub].filter(Boolean).join(" · ");
    $("#sum-when").textContent = ok
      ? `${d.toLocaleDateString("en-GB", { weekday: "short", day: "numeric", month: "short", year: "numeric" })} · ${d.toLocaleTimeString("en-GB", { hour: "2-digit", minute: "2-digit" })}`
      : "";
    $("#sum-where").textContent = [event?.venue?.name, event?.venue?.city].filter(Boolean).join(", ");
    $("#ticket-count").textContent = plural(tickets.length, "ticket");
    $("#order-number").textContent = order.order_number;
    $("#sum-seats").innerHTML = tickets.map((t) => {
      const [row, n] = String(t.seat_number || "").split("-");
      const seatText = t.seat_number ? `Row ${esc(row)} · Seat ${esc(n ?? "")}` : "General admission";
      return `<li class="sumseat">
          <span class="sumseat__info"><b>${esc(t.seat_zone)} · ${seatText}</b><small>${esc(t.seat_number || t.ticket_code)}</small></span>
          <span class="sumseat__end">${money(t.price)}</span>
        </li>`;
    }).join("");
    const same = tickets.length && tickets.every((t) => t.price === tickets[0].price);
    $("#sum-tickets-label").textContent = tickets.length ? `Tickets (${same ? `${tickets.length} × ${money(tickets[0].price)}` : tickets.length})` : "Tickets";
    $("#sum-tickets").textContent = money(order.total_amount);
    $("#sum-total").textContent = money(order.total_amount);
    $("#pay-label").textContent = `Pay ${money(order.total_amount)}`;
    $("#terms").textContent = `By selecting “Pay ${money(order.total_amount)}”, you agree to our Terms & Conditions and the venue’s entry policy. Tickets are non-refundable unless the event is cancelled.`;
    document.title = `Checkout · ${main} | ENCORE`;
  }

  // ---------- details ----------
  function prefill(user) {
    const saved = readJSON(DETAILS_KEY) || {};
    // saved-on-this-device first, then the account, so a fan's own edit wins
    $("#first-name").value = saved.first_name || user?.first_name || (user?.full_name || "").split(" ")[0] || "";
    $("#last-name").value = saved.last_name || user?.last_name || (user?.full_name || "").split(" ").slice(1).join(" ") || "";
    $("#email").value = saved.email || user?.email || "";
    if (saved.country) $("#country").value = saved.country;
    $("#postcode").value = saved.postcode || "";
    $("#card-name").value = [$("#first-name").value, $("#last-name").value].filter(Boolean).join(" ");
  }

  // ---------- 10-minute hold countdown ----------
  function startCountdown() {
    const end = order.expires_at ? new Date(order.expires_at.endsWith("Z") || /[+-]\d\d:\d\d$/.test(order.expires_at) ? order.expires_at : order.expires_at + "Z") : null;
    if (!end || Number.isNaN(end.getTime())) { $("#hold-badge").hidden = true; return; }
    const tick = () => {
      const left = Math.max(0, Math.round((end - Date.now()) / 1000));
      const mm = String(Math.floor(left / 60)).padStart(2, "0");
      const ss = String(left % 60).padStart(2, "0");
      const badge = $("#hold-badge");
      badge.textContent = `Seats held for ${mm}:${ss}`;
      badge.className = `badge ${left <= 60 ? "badge--out" : "badge--fast"}`;   // red in the last minute
      if (left === 60) badge.setAttribute("aria-live", "polite");               // announce once, not every second
      if (left === 0) { clearInterval(timer); showState("expired"); }
    };
    tick();
    timer = setInterval(tick, 1000);
  }

  // ---------- payment method tabs ----------
  const provider = () => document.querySelector('input[name="provider"]:checked').value;
  document.querySelectorAll('input[name="provider"]').forEach((r) => r.addEventListener("change", () => {
    document.querySelectorAll(".method-panel").forEach((p) => { p.hidden = p.dataset.for !== provider(); });
  }));

  // ---------- light input formatting ----------
  $("#card-number").addEventListener("input", (e) => {
    const digits = e.target.value.replace(/\D/g, "").slice(0, 19);
    e.target.value = digits.replace(/(\d{4})(?=\d)/g, "$1  ");
  });
  $("#card-exp").addEventListener("input", (e) => {
    const d = e.target.value.replace(/\D/g, "").slice(0, 4);
    e.target.value = d.length > 2 ? `${d.slice(0, 2)} / ${d.slice(2)}` : d;
  });
  $("#card-cvc").addEventListener("input", (e) => { e.target.value = e.target.value.replace(/\D/g, "").slice(0, 4); });

  // ---------- validation ----------
  function luhn(num) {
    let sum = 0;
    for (let i = 0; i < num.length; i++) {
      let d = Number(num[num.length - 1 - i]);
      if (i % 2) { d *= 2; if (d > 9) d -= 9; }
      sum += d;
    }
    return num.length >= 12 && sum % 10 === 0;
  }
  function validate() {
    const problems = [];
    const mark = (id, bad, msg) => { $(id).setAttribute("aria-invalid", String(bad)); if (bad) problems.push([id, msg]); };
    mark("#first-name", !$("#first-name").value.trim(), "Enter your first name.");
    mark("#last-name", !$("#last-name").value.trim(), "Enter your last name.");
    mark("#email", !/^[^\s@]+@[^\s@]+\.[^\s@]+$/.test($("#email").value.trim()), "Enter a valid email address.");
    if (provider() === "credit_card") {
      const [mm, yy] = $("#card-exp").value.split("/").map((s) => Number(s.trim()));
      const now = new Date();
      const expOk = mm >= 1 && mm <= 12 && yy >= 0 && new Date(2000 + yy, mm) > now;   // valid until the end of that month
      mark("#card-name", !$("#card-name").value.trim(), "Enter the name on the card.");
      mark("#card-number", !luhn($("#card-number").value.replace(/\D/g, "")), "Check the card number.");
      mark("#card-exp", !expOk, "Check the expiry date.");
      mark("#card-cvc", !/^\d{3,4}$/.test($("#card-cvc").value), "Enter the 3 or 4 digit security code.");
    } else {
      ["#card-name", "#card-number", "#card-exp", "#card-cvc"].forEach((id) => $(id).removeAttribute("aria-invalid"));
    }
    if (problems.length) { $(problems[0][0]).focus(); showPayError(problems[0][1]); }
    return problems.length === 0;
  }

  // ---------- pay ----------
  $("#pay-form").addEventListener("submit", async (ev) => {
    ev.preventDefault();
    $("#pay-error").hidden = true;
    if (!order || !validate()) return;

    if ($("#remember").checked) {
      writeJSON(DETAILS_KEY, {
        first_name: $("#first-name").value.trim(), last_name: $("#last-name").value.trim(), email: $("#email").value.trim(),
        country: $("#country").value, postcode: $("#postcode").value.trim(),
      });   // name/email/address only: never card data
    } else {
      try { localStorage.removeItem(DETAILS_KEY); } catch { /* ignore */ }
    }

    const btn = $("#pay-btn");
    btn.disabled = true;
    btn.setAttribute("aria-busy", "true");
    $("#pay-label").textContent = "Processing…";
    try {
      const res = await fetch(`${API_BASE}/orders/${order.id}/payments`, {
        method: "POST",
        headers: { "Content-Type": "application/json", Accept: "application/json" },
        body: JSON.stringify({
          amount: Number(order.total_amount),   // must equal the order total or the API answers 400
          provider: provider(),
          provider_tx_id: `DEMO-${order.order_number}-${Date.now().toString(36).toUpperCase()}`,
        }),
      });
      const body = await res.json().catch(() => ({}));
      if (res.ok) {
        const fresh = await getJSON(`/orders/${order.id}`);   // tickets are now "valid"
        if (fresh.ok) order = fresh.body;
        return showDone();
      }
      const detail = typeof body.detail === "string" ? body.detail : "";
      if (res.status === 410) return showState("expired");
      if (res.status === 409 && /already paid/i.test(detail)) { await load(); return; }   // paid in another tab
      if (res.status === 409 && /cancelled/i.test(detail)) return showState("cancelled");
      if (res.status === 503 || res.status === 409) {
        const wait = Number(res.headers.get("Retry-After")) || 2;
        return showPayError(`The payment service is busy. Your seats are still held — try again in ${wait} seconds.`);
      }
      showPayError(detail || "The payment didn’t go through. Your seats are still held — please try again.");
    } catch {
      showPayError("We couldn’t reach the payment service. Your seats are still held — please try again.");
    } finally {
      btn.removeAttribute("aria-busy");
      if (!$("#workspace").hidden && order?.status === "pending") {
        btn.disabled = false;
        $("#pay-label").textContent = `Pay ${money(order.total_amount)}`;
      }
    }
  });
  function showPayError(msg) {
    $("#pay-error").textContent = msg;
    $("#pay-error").hidden = false;
  }

  // ---------- confirmation (step 3) ----------
  function showDone() {
    clearInterval(timer);
    $("#workspace").hidden = true;
    $("#hold-badge").hidden = true;
    $("#step-2").className = "steps__item is-done";
    $("#step-2").removeAttribute("aria-current");
    $("#step-2").querySelector("span").textContent = "✓";
    $("#step-3").className = "steps__item is-current";
    $("#step-3").setAttribute("aria-current", "step");
    $("#page-title").textContent = "You’re going!";
    $("#page-lead").textContent = `Order ${order.order_number} · ${money(order.total_amount)} paid`;
    const { main, sub } = splitTitle(event);
    // no e-mail is sent by the API yet, so the page does not promise one
    $("#done-text").textContent = `${[main, sub].filter(Boolean).join(" · ")}. Show these ticket codes at the gate.`;
    $("#done-tickets").innerHTML = (order.tickets || []).map((t) => `
      <li class="ticket">
        <b>${esc(t.seat_zone)}${t.seat_number ? ` · ${esc(t.seat_number)}` : ""}</b>
        <small>${esc(t.status === "valid" ? "Valid ticket" : t.status)} · ${money(t.price)}</small>
        <code>${esc(t.ticket_code)}</code>
      </li>`).join("");
    $("#done").hidden = false;
    $("#done").scrollIntoView({ behavior: "smooth", block: "start" });
  }

  // ---------- promo code (no promo API yet) ----------
  $("#promo-form").addEventListener("submit", (e) => {
    e.preventDefault();
    const code = $("#promo").value.trim();
    const msg = $("#promo-msg");
    msg.hidden = false;
    msg.classList.toggle("is-error", Boolean(code));
    msg.textContent = code ? `“${code}” isn’t a valid code for this concert.` : "Enter a code first.";
  });

  // account hint: hidden when logged in, otherwise log in and come back to this order
  const hint = $(".account-hint");
  if (window.EncoreSession?.get()) hint.hidden = true;
  else hint.href = window.EncoreSession?.loginHref() || "../login/login.html";

  $("#print-btn").addEventListener("click", () => window.print());
  $("#retry-btn").addEventListener("click", load);
  load();
})();
