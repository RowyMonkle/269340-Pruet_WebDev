/* Backstage · Users
 *   GET /users?page=&page_size=                 -> paginated table (server side)
 *   GET /orders/summary?user_id=&limit=         -> bookings of one user, in the drawer
 *   GET /orders/{id}                            -> concert of each booking (first ticket)
 */
(() => {
  "use strict";
  const A = window.Admin;
  if (!A) return;
  const $ = (s) => document.querySelector(s);

  const view = { page: 1, size: 20, total: 0, pages: 1, role: "", q: "", items: [] };
  const BADGE = { confirmed: "badge--sale", paid: "badge--sale", pending: "badge--fast", expired: "badge--out", cancelled: "badge--out" };

  async function load() {
    $("#prev").disabled = $("#next").disabled = true;
    try {
      const res = await A.getJSON(`/users?page=${view.page}&page_size=${view.size}`);
      Object.assign(view, { items: res.items, total: res.total, pages: Math.max(1, res.total_pages) });
      render();
    } catch (err) {
      $("#rows").innerHTML = `<tr><td colspan="6" class="note">Couldn’t load users (${A.esc(err.message)}).</td></tr>`;
    } finally {
      $("#content").removeAttribute("aria-busy");
    }
  }

  const name = (u) => [u.first_name, u.last_name].filter(Boolean).join(" ") || u.full_name || "—";
  function render() {
    const q = view.q.toLowerCase();
    const rows = view.items.filter((u) => (!view.role || u.role === view.role)
      && (!q || [name(u), u.email, u.username].some((v) => String(v || "").toLowerCase().includes(q))));
    $("#rows").innerHTML = rows.map((u) => `
      <tr class="is-click" data-id="${u.id}" tabindex="0">
        <td class="num">${u.id}</td>
        <td class="cell-2"><b>${A.esc(name(u))}</b><small>${A.esc(u.email)}</small></td>
        <td>${A.esc(u.username)}</td>
        <td><span class="badge ${u.role === "admin" ? "badge--fast" : u.role === "organizer" ? "badge--sale" : "badge--plum"}">${A.esc(u.role)}</span></td>
        <td>${A.fmtDate(A.asDate(u.created_at))}</td>
        <td>${A.fmtDate(A.asDate(u.updated_at))}</td>
      </tr>`).join("") || `<tr><td colspan="6" class="note">No users on this page match.</td></tr>`;
    $("#count").textContent = `${A.num(view.total)} users · page ${view.page} of ${view.pages}`;
    $("#sub").textContent = `${A.num(view.total)} accounts. Click a row to see that person’s bookings.`;
    $("#prev").disabled = view.page <= 1;
    $("#next").disabled = view.page >= view.pages;
  }

  async function openUser(id) {
    const u = view.items.find((x) => x.id === id);
    if (!u) return;
    const body = A.openDrawer(`<h2>${A.esc(name(u))}</h2><p>${A.esc(u.email)} · ${A.esc(u.username)} · ${A.esc(u.role)} · joined ${A.fmtDate(A.asDate(u.created_at))}</p>`,
      `<p class="note">Loading bookings…</p>`);
    try {
      const orders = await A.getJSON(`/orders/summary?user_id=${id}&limit=50`);
      if (!orders.length) { body.innerHTML = `<p class="note">No bookings yet.</p>`; return; }
      const paid = orders.filter((o) => o.status === "confirmed" || o.status === "paid");
      body.innerHTML = `
        <p class="note"><b>${orders.length}${orders.length === 50 ? "+" : ""}</b> bookings · <b>${paid.length}</b> paid · ${A.money(paid.reduce((n, o) => n + Number(o.paid_amount || 0), 0), 2)} spent</p>
        <div class="table-wrap"><table class="table">
          <thead><tr><th>Order</th><th>Concert</th><th class="num">Tickets</th><th class="num">Amount</th><th>Status</th><th>Date</th></tr></thead>
          <tbody>${orders.map((o) => `
            <tr>
              <td>${A.esc(o.order_number)}</td>
              <td data-concert="${o.id}" class="note">…</td>
              <td class="num">${o.ticket_count}</td>
              <td class="num">${A.money(o.total_amount, 2)}</td>
              <td><span class="badge ${BADGE[o.status] || "badge--plum"}">${A.esc(o.status)}</span></td>
              <td>${A.fmtDate(A.asDate(o.created_at), true)}</td>
            </tr>`).join("")}</tbody>
        </table></div>`;
      // concert name per booking: order -> first ticket -> event (a few requests at a time)
      const eventNames = new Map();
      await A.pool(orders.slice(0, 20), 4, async (o) => {
        const d = await A.getJSON(`/orders/${o.id}`);
        const t = d.tickets?.[0];
        let label = "—";
        if (t) {
          if (!eventNames.has(t.event_id)) eventNames.set(t.event_id, A.getJSON(`/events/${encodeURIComponent(t.event_id)}`).then(A.title).catch(() => "Removed concert"));
          label = `${await eventNames.get(t.event_id)} · ${t.seat_zone}`;
        }
        const cell = body.querySelector(`[data-concert="${o.id}"]`);
        if (cell) { cell.textContent = label; cell.classList.remove("note"); }
      });
      body.querySelectorAll('[data-concert]').forEach((c) => { if (c.textContent === "…") c.textContent = ""; });
    } catch (err) {
      body.innerHTML = `<p class="note">Couldn’t load bookings (${A.esc(err.message)}).</p>`;
    }
  }

  $("#roles").addEventListener("click", (e) => {
    const c = e.target.closest("[data-r]"); if (!c) return;
    view.role = c.dataset.r;
    document.querySelectorAll("#roles .chip").forEach((x) => { const on = x === c; x.classList.toggle("is-active", on); x.setAttribute("aria-pressed", String(on)); });
    render();
  });
  $("#q").addEventListener("input", (e) => { view.q = e.target.value.trim(); render(); });
  $("#size").addEventListener("change", (e) => { view.size = Number(e.target.value); view.page = 1; load(); });
  $("#prev").addEventListener("click", () => { view.page--; load(); });
  $("#next").addEventListener("click", () => { view.page++; load(); });
  $("#rows").addEventListener("click", (e) => { const tr = e.target.closest("tr[data-id]"); if (tr) openUser(Number(tr.dataset.id)); });
  $("#rows").addEventListener("keydown", (e) => { const tr = e.target.closest("tr[data-id]"); if (tr && e.key === "Enter") openUser(Number(tr.dataset.id)); });

  load();
})();
