/* ENCORE log in + registration. One script for both pages: it looks for #login-form or #register-form.
 *
 * Log in   -> POST /api/v1/users/login { email, password }            -> 200 user | 401
 * Register -> POST /api/v1/users { email, username, password, first_name, last_name } -> 201 user | 409
 * On success the public user is kept by EncoreSession (session.js) and the fan goes back to ?next=.
 */
(() => {
  "use strict";

  const API_BASE = "/api/v1";
  const $ = (sel) => document.querySelector(sel);
  const Session = window.EncoreSession;

  // ---------- where to go afterwards ----------
  // only a path on this site is accepted ("/seats/seats.html?id=..."), never "//other.site" or "https://...",
  // so a crafted ?next= cannot send the fan to another domain after logging in
  function nextUrl() {
    const raw = new URLSearchParams(location.search).get("next") || "";
    return /^\/(?![\/\\])[^\s]*$/.test(raw) ? raw : "/";
  }
  // keep ?next= when switching between log in and register
  const next = new URLSearchParams(location.search).get("next");
  if (next && $("#switch-link")) $("#switch-link").href += `?next=${encodeURIComponent(next)}`;

  // already logged in? skip the form
  if (Session?.get() && $("#login-form")) {
    $("#login-sub").textContent = `You’re already logged in as ${Session.get().email}. Log in again to switch account.`;
  }

  // ---------- shared bits ----------
  function showError(html) {
    const box = $("#form-error");
    box.innerHTML = html;
    box.hidden = false;
  }
  function clearError() { $("#form-error").hidden = true; }
  function setBusy(busy, label) {
    const btn = $("#submit-btn");
    btn.disabled = busy;
    if (label) btn.firstChild.textContent = label + " ";
  }
  function mark(id, bad) { $(id).setAttribute("aria-invalid", String(bad)); return bad; }
  const validEmail = (s) => /^[^\s@]+@[^\s@]+\.[^\s@]+$/.test(s);
  const retryText = (res) => `Lots of fans are here right now. Please try again in ${Number(res.headers.get("Retry-After")) || 2} seconds.`;

  async function postJSON(path, body) {
    const res = await fetch(`${API_BASE}${path}`, {
      method: "POST",
      headers: { "Content-Type": "application/json", Accept: "application/json" },
      body: JSON.stringify(body),
    });
    return { res, body: await res.json().catch(() => ({})) };
  }

  // show / hide password
  document.querySelectorAll("[data-eye]").forEach((b) => b.addEventListener("click", () => {
    const input = document.getElementById(b.dataset.eye);
    const show = input.type === "password";
    input.type = show ? "text" : "password";
    b.setAttribute("aria-pressed", String(show));
    b.setAttribute("aria-label", show ? "Hide password" : "Show password");
  }));

  // features the API does not have yet (Google OAuth, password reset)
  document.querySelectorAll("[data-unavailable]").forEach((b) => b.addEventListener("click", () => {
    const box = $("#form-error");
    box.textContent = `${b.dataset.unavailable} isn’t available yet. Please use your email and password.`;
    box.hidden = false;
  }));

  // ======================= LOG IN =======================
  const loginForm = $("#login-form");
  if (loginForm) {
    loginForm.addEventListener("submit", async (e) => {
      e.preventDefault();
      clearError();
      const email = $("#email").value.trim();
      const password = $("#password").value;
      const bad = [mark("#email", !validEmail(email)), mark("#password", !password)];
      if (bad[0]) { $("#email").focus(); return showError("Enter the email address you signed up with."); }
      if (bad[1]) { $("#password").focus(); return showError("Enter your password."); }

      setBusy(true, "Logging in…");
      try {
        const { res, body } = await postJSON("/users/login", { email, password });
        if (res.ok) {
          Session.set(body, $("#remember").checked);
          location.href = nextUrl();
          return;
        }
        if (res.status === 401) {
          mark("#password", true);
          $("#password").select();
          return showError(`That email and password don’t match. New here? <a href="${$("#switch-link").getAttribute("href")}">Create an account</a>.`);
        }
        if (res.status === 503) return showError(retryText(res));
        if (res.status === 404 || res.status === 405) return showError("Log in isn’t switched on yet on this server (the /users/login endpoint is missing).");
        showError("Something went wrong. Please try again.");
      } catch {
        showError("We couldn’t reach ENCORE. Check your connection and try again.");
      } finally {
        setBusy(false, "Log in");
      }
    });
  }

  // ======================= REGISTER =======================
  const regForm = $("#register-form");
  if (regForm) {
    // password strength: 1 weak, 2 fair (meets the rule), 3 good, 4 strong
    function strength(pw) {
      if (!pw) return 0;
      const longEnough = pw.length >= 8;
      const hasNum = /\d/.test(pw);
      const hasSym = /[^A-Za-z0-9]/.test(pw);
      const mixed = /[a-z]/.test(pw) && /[A-Z]/.test(pw);
      if (!longEnough || !hasNum) return 1;   // below the rule
      if (!hasSym) return 2;                   // meets the rule
      return mixed || pw.length >= 12 ? 4 : 3; // symbol, plus mixed case or length
    }
    const STRENGTH_TEXT = [
      "Use 8+ characters with a number. Add a symbol to make it strong.",
      "Too weak · use 8+ characters and at least one number",
      "Fair password · add a symbol to make it stronger",
      "Good password · 8+ characters, a number and a symbol",
      "Strong password · 8+ characters, a number and a symbol",
    ];
    $("#password").addEventListener("input", () => {
      const lvl = Math.min(4, strength($("#password").value));
      $("#strength").dataset.level = String(lvl);
      $("#strength-text").textContent = STRENGTH_TEXT[lvl];
      if (lvl >= 2) $("#password").removeAttribute("aria-invalid");
    });

    // The API needs a unique username (3-50 of a-z 0-9 _ -) but the design does not ask for one:
    // build it from the email, with a short random suffix so two "jamie@..." accounts do not collide.
    function makeUsername(email) {
      const base = email.split("@")[0].replace(/[^a-zA-Z0-9_-]/g, "").slice(0, 40) || "fan";
      return `${base.length < 3 ? base + "fan" : base}_${Math.random().toString(36).slice(2, 6)}`;
    }

    regForm.addEventListener("submit", async (e) => {
      e.preventDefault();
      clearError();
      const first = $("#first-name").value.trim();
      const last = $("#last-name").value.trim();
      const email = $("#email").value.trim();
      const password = $("#password").value;
      const problems = [
        mark("#first-name", !first) && ["#first-name", "Enter your first name."],
        mark("#email", !validEmail(email)) && ["#email", "Enter a valid email address."],
        mark("#password", strength(password) < 2) && ["#password", "Your password needs 8+ characters and at least one number."],
      ].filter(Boolean);
      if (problems.length) { $(problems[0][0]).focus(); return showError(problems[0][1]); }

      setBusy(true, "Creating your account…");
      try {
        let attempt = 0;
        let res, body;
        do {   // retry once with a new suffix if the generated username is taken
          ({ res, body } = await postJSON("/users", {
            email, password, username: makeUsername(email), first_name: first, last_name: last || null,
          }));
          attempt++;
        } while (res.status === 409 && /username/i.test(body.detail || "") && attempt < 3);

        if (res.status === 201) {
          Session.set(body, true);   // a new account stays logged in on this device
          location.href = nextUrl();
          return;
        }
        if (res.status === 409) {
          mark("#email", true);
          return showError(`There’s already an account for ${email.replace(/[<>&"]/g, "")}. <a href="../login/login.html${next ? `?next=${encodeURIComponent(next)}` : ""}">Log in instead</a>.`);
        }
        if (res.status === 400) return showError("Please check your details and try again.");
        if (res.status === 503) return showError(retryText(res));
        showError("Something went wrong. Please try again.");
      } catch {
        showError("We couldn’t reach ENCORE. Check your connection and try again.");
      } finally {
        setBusy(false, "Create my account");
      }
    });
  }
})();
