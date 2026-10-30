/* Backstage · Workspace settings: GET /admin/flags, PUT /admin/flags/{key} with X-Admin-Token. */
(() => {
  "use strict";
  const A = window.Admin;
  if (!A) return;
  const $ = (s) => document.querySelector(s);

  const DESCRIBE = {
    user_name_write_mode: "Which name columns new and updated users are written to (legacy = full_name, dual = both, new = first/last).",
    user_name_read_source: "Which columns the API reads names from (legacy = full_name, new = first_name/last_name).",
  };

  function tokenError(err) {
    const box = $("#token-msg");
    box.hidden = false;
    box.textContent = err.status === 401 ? "That token is wrong."
      : err.status === 503 ? "Admin API is switched off: ADMIN_TOKEN is not set in .env."
      : `Couldn’t reach the admin API (${err.message}).`;
    if (err.status === 401) A.token.set("");
  }

  async function loadFlags() {
    const t = A.token.get();
    if (!t) return;
    $("#token-msg").hidden = true;
    try {
      const res = await A.getJSON("/admin/flags", { headers: { "X-Admin-Token": t } });
      $("#flags").innerHTML = `<div class="table-wrap"><table class="table"><thead><tr><th>Flag</th><th>Value</th><th></th></tr></thead><tbody>
        ${Object.entries(res.flags).map(([k, v]) => `
          <tr>
            <td class="cell-2"><b>${A.esc(k)}</b><small>${A.esc(DESCRIBE[k] || "")}</small></td>
            <td><select class="finput" data-flag="${A.esc(k)}" aria-label="${A.esc(k)}">
              ${(res.choices[k] || [v]).map((c) => `<option ${c === v ? "selected" : ""}>${A.esc(c)}</option>`).join("")}
            </select></td>
            <td><button type="button" class="btn btn--outline" data-save="${A.esc(k)}">Save</button></td>
          </tr>`).join("")}
      </tbody></table></div>`;
    } catch (err) {
      $("#flags").innerHTML = `<p class="note">Enter the admin token to load the flags.</p>`;
      tokenError(err);
    }
  }

  $("#flags").addEventListener("click", async (e) => {
    const b = e.target.closest("[data-save]");
    if (!b) return;
    const key = b.dataset.save;
    const value = document.querySelector(`[data-flag="${CSS.escape(key)}"]`).value;
    const msg = $("#flag-msg");
    b.disabled = true;
    try {
      const r = await A.getJSON(`/admin/flags/${encodeURIComponent(key)}`, {
        method: "PUT",
        headers: { "Content-Type": "application/json", "X-Admin-Token": A.token.get() },
        body: JSON.stringify({ value }),
      });
      msg.className = "msg"; msg.hidden = false;
      msg.textContent = `${r.key} is now “${r.value}” (${A.fmtDate(A.asDate(r.updated_at), true)}).`;
    } catch (err) {
      msg.className = "msg is-error"; msg.hidden = false;
      msg.textContent = err.status === 401 ? "The token stopped working. Enter it again." : `Couldn’t save: ${err.message}`;
    } finally {
      b.disabled = false;
    }
  });

  $("#token-form").addEventListener("submit", (e) => {
    e.preventDefault();
    const v = $("#token").value.trim();
    if (!v) return;
    A.token.set(v);
    $("#token").value = "";
    loadFlags();
  });
  $("#forget").addEventListener("click", () => {
    A.token.set("");
    $("#flags").innerHTML = `<p class="note">Enter the admin token to load the flags.</p>`;
  });

  loadFlags();
})();
