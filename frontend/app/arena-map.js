/* ENCORE seat plan: a generic arena (floor + level 2 + level 3), drawn as one SVG in ENCORE's own style.
 * The layout is our own simplified drawing, not a copy of any venue's official plan.
 *
 * Zone NAMES here must match the zones in MongoDB (seed.py ARENA_ZONES). Prices, capacity and
 * booked seats come from the API; this file only knows where each zone is drawn.
 *   EncoreArena.fits(zones)                      -> true when the event uses this plan
 *   EncoreArena.tiers(zones)                     -> [{price, color, zones:[...]}] most expensive first
 *   EncoreArena.render(el, zones, {selected, onPick, highlightPrice, compact})
 */
(() => {
  "use strict";

  // colours of the 8 price tiers, most expensive first (ENCORE palette: plum -> mint -> gold -> lilac)
  const TIER_COLORS = ["#583d62", "#82638c", "#2f7a68", "#5fa592", "#b8913f", "#d9bd80", "#a996b2", "#ddd1e2"];
  const TIER_TEXT = ["#fff", "#fff", "#fff", "#fff", "#fff", "#24182d", "#fff", "#24182d"];

  const W = 1000, H = 912;               // tall enough for the rear rows at the back (CY + 430 = 900)
  const CX = 500, CY = 470;              // centre of the curved back of the arena
  const R2 = [225, 300];                 // level 2 ring (inner, outer radius)
  const R3 = [318, 388], R3R = [392, 430]; // level 3 front rows / rear rows
  const SHAPES = {};

  // rounded rectangle path (r = corner radius)
  const rect = (x, y, w, h, r = 8) => {
    r = Math.min(r, w / 2, h / 2);
    return `M${x + r},${y}H${x + w - r}Q${x + w},${y} ${x + w},${y + r}V${y + h - r}Q${x + w},${y + h} ${x + w - r},${y + h}H${x + r}Q${x},${y + h} ${x},${y + h - r}V${y + r}Q${x},${y} ${x + r},${y}Z`;
  };
  function sector(r0, r1, a0, a1) {     // ring piece between angles a0..a1 (degrees, 0 = right, 90 = down)
    const pts = [];
    const step = Math.max(2, (a1 - a0) / 10);
    for (let a = a0; a <= a1 + 0.01; a += step) pts.push(pol(r1, a));
    for (let a = a1; a >= a0 - 0.01; a -= step) pts.push(pol(r0, a));
    return "M" + pts.map((p) => p.join(",")).join("L") + "z";
  }
  function pol(r, a) { const t = (a * Math.PI) / 180; return [+(CX + r * Math.cos(t)).toFixed(1), +(CY + r * Math.sin(t)).toFixed(1)]; }
  function mid(r, a) { return pol(r, a); }

  // ---------- floor ----------
  const F = { x: 300, y: 120, w: 400 };
  SHAPES.A1 = { d: rect(310, 140, 55, 210), at: [337, 245] };
  SHAPES.A2 = { d: rect(372, 140, 120, 210), at: [432, 245] };
  SHAPES.A3 = { d: rect(508, 140, 120, 210), at: [568, 245] };
  SHAPES.A4 = { d: rect(635, 140, 55, 210), at: [662, 245] };
  SHAPES.B1 = { d: rect(310, 370, 55, 85), at: [337, 412] };
  SHAPES.B2 = { d: rect(370, 370, 260, 85), at: [500, 420] };
  SHAPES.B3 = { d: rect(635, 370, 55, 85), at: [662, 412] };
  SHAPES.C1 = { d: rect(310, 462, 55, 40), at: [337, 482] };
  SHAPES.C2 = { d: rect(370, 462, 260, 40), at: [500, 482] };
  SHAPES.C3 = { d: rect(635, 462, 55, 40), at: [662, 482] };
  SHAPES.FF = { d: rect(420, 512, 50, 34), at: [445, 529] };
  SHAPES.HH = { d: rect(530, 512, 50, 34), at: [555, 529] };

  // ---------- level 2 ----------
  const L2x = [CX - R2[1], CX - R2[0]];   // left column x range
  const R2x = [CX + R2[0], CX + R2[1]];
  const side2 = [["SB", 120, 185], ["SC", 190, 280], ["SD", 285, 375], ["SE", 380, CY]];
  side2.forEach(([n, y0, y1]) => { SHAPES[n] = { d: rect(L2x[0], y0, L2x[1] - L2x[0], y1 - y0), at: [(L2x[0] + L2x[1]) / 2, (y0 + y1) / 2] }; });
  const side2R = [["SN", 120, 185], ["SM", 190, 280], ["SL", 285, 375], ["SK", 380, CY]];
  side2R.forEach(([n, y0, y1]) => { SHAPES[n] = { d: rect(R2x[0], y0, R2x[1] - R2x[0], y1 - y0), at: [(R2x[0] + R2x[1]) / 2, (y0 + y1) / 2] }; });
  SHAPES.SB.note = SHAPES.SN.note = "RESTRICTED VIEW";
  const curve2 = ["SJ", "SI", "SH", "SG", "SF"];   // right -> left along the back
  curve2.forEach((n, i) => {
    const a0 = i * 36 + 1, a1 = (i + 1) * 36 - 1;
    SHAPES[n] = { d: sector(R2[0], R2[1], a0, a1), at: mid((R2[0] + R2[1]) / 2, (a0 + a1) / 2) };
  });

  // ---------- level 3 (front rows + rear rows per block) ----------
  const L3 = [CX - R3[1], CX - R3[0]], L3r = [CX - R3R[1], CX - R3R[0]];
  const R3x = [CX + R3[0], CX + R3[1]], R3rx = [CX + R3R[0], CX + R3R[1]];
  const sides3 = [["C", 150, 250], ["D", 255, 360], ["E", 365, CY]];
  sides3.forEach(([n, y0, y1]) => {
    SHAPES[n] = { d: rect(L3[0], y0, L3[1] - L3[0], y1 - y0), at: [(L3[0] + L3[1]) / 2, (y0 + y1) / 2] };
    SHAPES[`${n} Rear`] = { d: rect(L3r[0], y0, L3r[1] - L3r[0], y1 - y0), at: [(L3r[0] + L3r[1]) / 2, (y0 + y1) / 2], small: true };
  });
  const sides3R = [["S", 150, 250], ["R", 255, 360], ["Q", 365, CY]];
  sides3R.forEach(([n, y0, y1]) => {
    SHAPES[n] = { d: rect(R3x[0], y0, R3x[1] - R3x[0], y1 - y0), at: [(R3x[0] + R3x[1]) / 2, (y0 + y1) / 2] };
    SHAPES[`${n} Rear`] = { d: rect(R3rx[0], y0, R3rx[1] - R3rx[0], y1 - y0), at: [(R3rx[0] + R3rx[1]) / 2, (y0 + y1) / 2], small: true };
  });
  const curve3 = ["P", "O", "N", "M", "L", "K", "J", "I", "H", "G", "F"];   // right -> left
  const step3 = 180 / curve3.length;
  curve3.forEach((n, i) => {
    const a0 = i * step3 + 0.8, a1 = (i + 1) * step3 - 0.8;
    SHAPES[n] = { d: sector(R3[0], R3[1], a0, a1), at: mid((R3[0] + R3[1]) / 2, (a0 + a1) / 2) };
    SHAPES[`${n} Rear`] = { d: sector(R3R[0], R3R[1], a0, a1), at: mid((R3R[0] + R3R[1]) / 2, (a0 + a1) / 2), small: true };
  });

  const NAMES = Object.keys(SHAPES);

  // ---------- public API ----------
  function fits(zones) {
    const names = new Set((zones || []).map((z) => z.name));
    return names.size > 10 && [...names].every((n) => SHAPES[n]);
  }
  function tiers(zones) {
    const prices = [...new Set((zones || []).map((z) => Number(z.price)))].sort((a, b) => b - a);
    return prices.map((price, i) => ({
      price,
      color: TIER_COLORS[Math.min(i, TIER_COLORS.length - 1)],
      text: TIER_TEXT[Math.min(i, TIER_TEXT.length - 1)],
      zones: zones.filter((z) => Number(z.price) === price),
    }));
  }
  const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));

  function render(el, zones, opts = {}) {
    const tierOf = new Map();
    tiers(zones).forEach((t) => t.zones.forEach((z) => tierOf.set(z.name, t)));
    const byName = new Map(zones.map((z) => [z.name, z]));
    const parts = [];
    // outlines
    parts.push(`<path d="M320,96Q500,58 680,96V40Q680,22 662,22H338Q320,22 320,40Z" class="am-stage"/>`);
    parts.push(`<text x="500" y="68" class="am-stage-t">STAGE</text>`);
    parts.push(`<path d="${rect(F.x, F.y, F.w, 440, 18)}" class="am-floor"/>`);
    parts.push(`<text x="500" y="368" class="am-floor-t">FLOOR</text>`);
    parts.push(`<rect x="476" y="512" width="48" height="34" rx="6" class="am-foh"/><text x="500" y="533" class="am-foh-t">FOH</text>`);
    parts.push(`<text x="${L2x[0]}" y="108" class="am-level">LEVEL 2</text><text x="${R2x[1]}" y="108" class="am-level am-level--end">LEVEL 2</text>`);
    parts.push(`<text x="${L3r[0]}" y="138" class="am-level">LEVEL 3</text><text x="${R3rx[1]}" y="138" class="am-level am-level--end">LEVEL 3</text>`);

    for (const name of NAMES) {
      const z = byName.get(name);
      const s = SHAPES[name];
      if (!z) { parts.push(`<path d="${s.d}" class="am-zone am-zone--none"/>`); continue; }
      const t = tierOf.get(name);
      const left = Math.max(0, z.capacity - z.booked_count);
      const out = left === 0 || opts.locked;
      const dim = opts.highlightPrice != null && Number(z.price) !== opts.highlightPrice;
      const label = s.small ? "" : name;
      parts.push(`<g class="am-z${out ? " is-out" : ""}${opts.selected === name ? " is-sel" : ""}${dim ? " is-dim" : ""}" data-zone="${esc(name)}"
          role="button" tabindex="${out ? -1 : 0}" aria-pressed="${opts.selected === name}" aria-disabled="${out}"
          aria-label="${esc(name)}, ${t.price.toLocaleString("en-US")} baht, ${out ? "not available" : left + " seats left"}">
        <title>${esc(name)} · ฿${t.price.toLocaleString("en-US")} · ${out ? "sold out" : left.toLocaleString("en-US") + " left"}</title>
        <path d="${s.d}" fill="${t.color}" class="am-zone"/>
        ${label ? `<text x="${s.at[0]}" y="${s.at[1] + (s.note ? -2 : 5)}" fill="${t.text}" class="am-zt">${esc(label)}</text>` : ""}
        ${s.note ? `<text x="${s.at[0]}" y="${s.at[1] + 12}" fill="${t.text}" class="am-note">${s.note}</text>` : ""}
      </g>`);
    }
    el.innerHTML = `<svg viewBox="0 0 ${W} ${H}" class="arena-map" role="group" aria-label="Arena seat plan">${parts.join("")}</svg>`;

    if (opts.onPick) {
      const pick = (g) => { if (g && g.getAttribute("aria-disabled") !== "true") opts.onPick(g.dataset.zone); };
      el.onclick = (e) => pick(e.target.closest("[data-zone]"));
      el.onkeydown = (e) => { if (e.key === "Enter" || e.key === " ") { e.preventDefault(); pick(e.target.closest("[data-zone]")); } };
    }
  }

  const levelOf = (name) => (/^S[A-Z]$/.test(name) ? "Level 2" : /^[A-Z]( Rear)?$/.test(name) ? (/ Rear$/.test(name) ? "Level 3 · rear rows" : "Level 3") : "Floor");
  window.EncoreArena = { fits, tiers, render, levelOf, colors: TIER_COLORS };
})();
