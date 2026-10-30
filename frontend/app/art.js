/* ENCORE artist artwork.
 *
 * Official artist photos are NOT shipped with the site. To use your own licensed images:
 *   1. save them in frontend/app/assets/artists/ named after the artist: bts.jpg, nct-dream.png, blackpink.webp ...
 *   2. run frontend/app/update-artist-photos.ps1 (rewrites photos.json with the files it finds)
 * and every page picks them up (the list avoids asking the server for photos that do not exist). Until then each artist gets a generated typographic poster
 * (colour from the artist name, so it is stable and different per act).
 *
 *   EncoreArt.slug("NCT DREAM")          -> "nct-dream"
 *   EncoreArt.poster("NCT DREAM", "wide") -> data: URL of an SVG poster
 *   EncoreArt.apply(img, "NCT DREAM", "wide" | "square")  -> tries the photo, falls back to the poster
 */
(() => {
  "use strict";

  const ROOT = "/assets/artists/";
  const PALETTES = [   // [from, to, accent] dark gradients that keep white text readable
    ["#24182d", "#583d62", "#c1e4dd"], ["#1b2a4a", "#4e5fa8", "#8ec3e8"], ["#3a1238", "#a3406f", "#f7c4d6"],
    ["#14302b", "#2f7a68", "#c1e4dd"], ["#2b1d0e", "#8a5a1c", "#f2d49b"], ["#1f1640", "#6a3fa0", "#d9c8ff"],
    ["#2a0f16", "#9b2c2c", "#ffd1c4"], ["#0f2433", "#1f6f8b", "#bfe8f5"],
  ];

  const slug = (name) => String(name || "artist").toLowerCase().normalize("NFKD").replace(/[^a-z0-9]+/g, "-").replace(/^-|-$/g, "") || "artist";
  function hash(s) { let h = 2166136261; for (const c of s) { h ^= c.charCodeAt(0); h = Math.imul(h, 16777619); } return h >>> 0; }
  const xmlEsc = (s) => String(s).replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&apos;" }[c]));

  const cache = new Map();
  function poster(name, shape = "wide") {
    const key = `${name}|${shape}`;
    if (cache.has(key)) return cache.get(key);
    const h = hash(slug(name));
    const [c0, c1, acc] = PALETTES[h % PALETTES.length];
    const [w, hgt] = shape === "square" ? [600, 600] : shape === "tall" ? [600, 800] : [1200, 800];
    const text = String(name || "");   // keep the act's own casing (aespa, NewJeans)
    const size = Math.min(hgt * 0.24, (w * 0.84) / Math.max(3, text.length * 0.72));   // fits the width
    const angle = (h >> 4) % 60 - 30;
    // light beams + rings: abstract "stage" art, no likeness of anyone
    const beams = [0, 1, 2, 3].map((i) => {
      const x = (w * (0.15 + 0.23 * i) + (h >> (i + 2)) % 80).toFixed(0);
      return `<polygon points="${x},0 ${+x + 40},0 ${+x + 220 - i * 90},${hgt} ${+x - 120 + i * 40},${hgt}" fill="${acc}" opacity="${0.05 + (i % 2) * 0.04}"/>`;
    }).join("");
    const svg = `<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 ${w} ${hgt}" preserveAspectRatio="xMidYMid slice">
      <defs><linearGradient id="g" gradientTransform="rotate(${angle} .5 .5)"><stop offset="0" stop-color="${c0}"/><stop offset="1" stop-color="${c1}"/></linearGradient>
      <radialGradient id="r" cx=".5" cy="1" r=".8"><stop offset="0" stop-color="${acc}" stop-opacity=".35"/><stop offset="1" stop-color="${acc}" stop-opacity="0"/></radialGradient></defs>
      <rect width="${w}" height="${hgt}" fill="url(#g)"/>${beams}<rect width="${w}" height="${hgt}" fill="url(#r)"/>
      <circle cx="${w * 0.8}" cy="${hgt * 0.22}" r="${hgt * 0.16}" fill="none" stroke="${acc}" stroke-opacity=".35" stroke-width="2"/>
      <circle cx="${w * 0.8}" cy="${hgt * 0.22}" r="${hgt * 0.25}" fill="none" stroke="${acc}" stroke-opacity=".18" stroke-width="2"/>
      <text x="${w * 0.08}" y="${hgt * (shape === "square" ? 0.72 : 0.66)}" font-family="Bodoni Moda, Didot, Georgia, serif" font-size="${size.toFixed(0)}" font-weight="500" fill="#fff" letter-spacing="2">${xmlEsc(text)}</text>
      <text x="${w * 0.08 + 4}" y="${hgt * (shape === "square" ? 0.72 : 0.66) - size * 0.95}" font-family="Inter, Arial, sans-serif" font-size="${Math.max(14, hgt * 0.035).toFixed(0)}" font-weight="700" fill="${acc}" letter-spacing="4">LIVE IN BANGKOK</text>
    </svg>`;
    const url = "data:image/svg+xml;charset=utf-8," + encodeURIComponent(svg);
    cache.set(key, url);
    return url;
  }

  // which artists have a real photo (assets/artists/photos.json), loaded once per page
  let photoList = null;
  const photos = () => (photoList ||= fetch(`${ROOT}photos.json`, { cache: "no-cache" })
    .then((r) => (r.ok ? r.json() : []))
    // entries are file names ("bts.jpg"); a bare slug ("bts") means bts.jpg
    .then((list) => new Map((Array.isArray(list) ? list : []).map((f) => [String(f).replace(/\.(jpe?g|png|webp)$/i, ""), /\./.test(f) ? f : `${f}.jpg`])))
    .catch(() => new Map()));

  function apply(img, name, shape = "wide") {
    if (!img) return;
    const s = slug(name);
    img.alt = img.alt || "";
    img.src = poster(name, shape);                       // instant: never an empty box
    photos().then((have) => {
      if (!have.has(s)) return;
      img.src = ROOT + encodeURIComponent(have.get(s));
      img.style.objectPosition = "50% 25%";   // group photos: keep faces (upper part) when the box crops the picture
    });
  }
  // markup helper for pages that build HTML strings: <img data-artist="BTS" data-shape="wide">
  function hydrate(root = document) {
    root.querySelectorAll("img[data-artist]").forEach((img) => { if (!img.dataset.done) { img.dataset.done = "1"; apply(img, img.dataset.artist, img.dataset.shape || "wide"); } });
  }

  window.EncoreArt = { slug, poster, apply, hydrate };
})();
