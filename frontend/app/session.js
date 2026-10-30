/* ENCORE session helper, loaded on every page BEFORE the page script.
 *
 * The API has no tokens yet: POST /users/login checks the password and returns the user,
 * and the browser keeps that public user record here.
 *   "Keep me logged in" ticked   -> localStorage   (survives closing the browser)
 *   not ticked                   -> sessionStorage (gone when the tab closes)
 * It also turns the nav's "Log in / Sign up" button into "Hi, <name>" + "Log out".
 */
(() => {
  "use strict";
  const KEY = "encore.user";

  function read(store) {
    try { return JSON.parse(store.getItem(KEY)); } catch { return null; }
  }
  const Session = {
    get() {
      return read(sessionStorage) || read(localStorage);   // the tab's own login wins
    },
    set(user, remember) {
      // keep only what the pages need; never anything secret
      const slim = { id: user.id, email: user.email, first_name: user.first_name || (user.full_name || "").split(" ")[0] || user.username, role: user.role };
      try {
        (remember ? localStorage : sessionStorage).setItem(KEY, JSON.stringify(slim));
        (remember ? sessionStorage : localStorage).removeItem(KEY);
      } catch { /* storage blocked: the login only lasts for this page */ }
      return slim;
    },
    clear() {
      try { localStorage.removeItem(KEY); sessionStorage.removeItem(KEY); } catch { /* ignore */ }
    },
    // log-in page that sends the fan back here afterwards (absolute paths: works from any page folder)
    loginHref(next = location.pathname + location.search) {
      return `/login/login.html?next=${encodeURIComponent(next || "/")}`;
    },
  };
  window.EncoreSession = Session;

  // ---------- nav button ----------
  function paintNav() {
    const btn = document.querySelector(".nav__actions .btn--outline");
    if (!btn) return;
    const user = Session.get();
    if (!user) {
      btn.textContent = "Log in / Sign up";
      btn.href = Session.loginHref();
      return;
    }
    btn.textContent = "Log out";
    btn.href = "#logout";
    btn.addEventListener("click", (e) => {
      e.preventDefault();
      Session.clear();
      location.reload();
    }, { once: true });
    if (!document.querySelector(".nav__hello")) {
      const hello = document.createElement("a");   // the greeting opens the fan's own dashboard
      hello.className = "nav__hello";
      hello.href = "/account/account.html";
      hello.textContent = `Hi, ${user.first_name || "there"}`;
      btn.before(hello);
    }
    if ((user.role === "admin" || user.role === "organizer") && !document.querySelector(".nav__admin")) {
      const admin = document.createElement("a");   // admins and organizers get a way into Backstage
      admin.className = "btn btn--outline nav__admin";
      admin.href = user.role === "admin" ? "/admin/index.html" : "/admin/concerts.html";   // organizers: concerts only
      admin.textContent = user.role === "admin" ? "Admin" : "Backstage";
      btn.before(admin);
    }
  }

  // "Your tickets" in every menu and footer -> the dashboard (log in first when needed)
  function wireTicketLinks() {
    document.querySelectorAll("a").forEach((a) => {
      if (a.textContent.trim() !== "Your tickets") return;
      a.href = Session.get() ? "/account/account.html" : Session.loginHref("/account/account.html");
    });
  }
  const start = () => { paintNav(); wireTicketLinks(); };
  if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", start);
  else start();
})();
