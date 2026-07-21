const VAPID_PUBLIC_KEY = "BDKwnexe_jAsbln6CFqhe9qMnjyh3tsOsIW5YcV9UN39-E7kjRjHGJsJAnhkT4k8Z8pCm5edQnGrNX8Icx4WENM";
const $ = (s) => document.querySelector(s);
let feed = [];

// ---------- theme (follows the system; ?theme=dark|light overrides) ----------
{
  const t = new URLSearchParams(location.search).get("theme") || localStorage.getItem("theme");
  if (t === "dark" || t === "light") document.documentElement.dataset.theme = t;
}

// ---------- tabs ----------
document.querySelectorAll("nav button").forEach((b) =>
  b.addEventListener("click", () => {
    document.querySelectorAll("nav button").forEach((x) => x.classList.toggle("active", x === b));
    document.querySelectorAll(".tab").forEach((t) => t.classList.toggle("active", t.id === "tab-" + b.dataset.tab));
    if (b.dataset.tab === "clubs") renderClubs();
  })
);

// ---------- helpers ----------
const known = (v) => v && v.trim() !== "" && v.trim() !== "—";
const esc = (s) =>
  String(s).replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;").replace(/"/g, "&quot;");

// Same alias canonicalization as the bot, so "Barça"/"FC Barcelona" group together.
const CLUB_CANON = [
  ["Real Madrid", /real madrid/],
  ["Barcelona", /barcelona|\bbarca\b/],
  ["Atlético Madrid", /atletico/],
  ["Arsenal", /arsenal/],
  ["Chelsea", /chelsea/],
  ["Liverpool", /liverpool/],
  ["Manchester City", /man(chester)? city/],
  ["Manchester United", /man(chester)? u(ni)?te?d/],
  ["Tottenham", /tottenham|\bspurs\b/],
  ["Bayern Munich", /bayern/],
  ["Borussia Dortmund", /dortmund/],
  ["PSG", /paris saint[- ]germain|\bpsg\b/],
  ["Juventus", /juventus|\bjuve\b/],
  ["Inter", /\binter\b(?!\s+miami)/],
  ["AC Milan", /\bmilan\b/],
  ["Napoli", /napoli/],
];

function canonClub(raw) {
  const n = raw.trim().normalize("NFD").replace(/\p{Diacritic}/gu, "").toLowerCase();
  for (const [pretty, re] of CLUB_CANON) if (re.test(n)) return pretty;
  return raw.trim().replace(/\s+(FC|CF|AFC)$/i, "");
}

function clubsOf(i) {
  const out = new Set();
  if (known(i.to_club)) i.to_club.split(",").forEach((c) => known(c) && out.add(canonClub(c)));
  if (known(i.from_club)) out.add(canonClub(i.from_club));
  return [...out];
}

const stageOf = (i) => (i.kind === "interest" ? "rumour" : known(i.stage) ? i.stage : "Completed");

function matchesStage(i, want) {
  if (!want) return true;
  return stageOf(i) === want;
}

const PALETTE = ["#c0392b", "#1a5fb4", "#1c7c43", "#7b2d8b", "#b8860b", "#0f7173", "#a13d63", "#34495e", "#d35400", "#2d6a4f"];
// real colors for the watched clubs (white-text-safe tones); others hash into the palette
const CLUB_COLORS = {
  "Real Madrid": "#b09037", "Barcelona": "#a50044", "Atlético Madrid": "#cb3524",
  "Arsenal": "#c00a1d", "Chelsea": "#0348a4", "Liverpool": "#c8102e",
  "Manchester City": "#2a7fbc", "Manchester United": "#b0201a", "Tottenham": "#131f3c",
  "Bayern Munich": "#b00520", "Borussia Dortmund": "#a08000", "PSG": "#004170",
  "Juventus": "#26282a", "Inter": "#0068a8", "AC Milan": "#ac1620", "Napoli": "#0f7fb0",
};
// official crests via football-data.org's public CDN (hotlinked, not
// committed — trademarked artwork stays out of the repo); the colored
// monogram beneath doubles as the automatic fallback
const CLUB_CRESTS = {
  "Arsenal": 57, "Chelsea": 61, "Liverpool": 64, "Manchester City": 65,
  "Manchester United": 66, "Tottenham": 73, "Atlético Madrid": 78,
  "Barcelona": 81, "Real Madrid": 86, "Bayern Munich": 5,
  "Borussia Dortmund": 4, "PSG": 524, "Juventus": 109, "Inter": 108,
  "AC Milan": 98, "Napoli": 113,
};
function avatarHTML(club) {
  const words = club.replace(/[^\p{L}\s]/gu, "").split(/\s+/).filter(Boolean);
  const ini = (words.length >= 2 ? words[0][0] + words[1][0] : club.slice(0, 3)).toUpperCase();
  const canon = canonClub(club);
  let color = CLUB_COLORS[canon];
  if (!color) {
    let h = 0;
    for (const ch of canon) h = (h * 31 + ch.charCodeAt(0)) >>> 0;
    color = PALETTE[h % PALETTE.length];
  }
  const crest = CLUB_CRESTS[canon];
  const img = crest
    ? `<img src="https://crests.football-data.org/${crest}.png" alt="" loading="lazy" onerror="this.remove()">`
    : "";
  return `<div class="ava" style="background:${color}"><span>${esc(ini)}</span>${img}</div>`;
}

function dayLabel(ts) {
  const d = new Date(ts);
  const days = Math.floor((new Date().setHours(0, 0, 0, 0) - new Date(d).setHours(0, 0, 0, 0)) / 864e5);
  if (days <= 0) return "Today";
  if (days === 1) return "Yesterday";
  return d.toLocaleDateString("en-GB", { weekday: "long", day: "numeric", month: "short" });
}

function stageBadge(i) {
  if (i.kind === "interest") return '<span class="badge s-rumour">👀 Rumour</span>';
  const map = { "Completed": ["s-completed", "✓ Completed"], "Here we go": ["s-herewego", "Here we go"], "Medical": ["s-medical", "Medical"] };
  const [cls, txt] = map[i.stage] || ["s-completed", "Deal"];
  return `<span class="badge ${cls}">${txt}</span>`;
}

// ---------- cards ----------
function cardHTML(i, forClub) {
  const isRumour = i.kind === "interest";
  const mainClub = isRumour ? i.to_club.split(",")[0] : (known(i.to_club) ? i.to_club : i.from_club);
  const move = isRumour
    ? `${esc(i.to_club)} in for the ${known(i.from_club) ? esc(i.from_club) + " " : ""}man`
    : `${esc(i.from_club)} → ${esc(i.to_club)}`;
  let inout = "";
  if (forClub && !isRumour) {
    const joined = known(i.to_club) && canonClub(i.to_club) === forClub;
    inout = `<span class="badge ${joined ? "in" : "out"}">${joined ? "⬅ In" : "➡ Out"}</span>`;
  }
  const meta = [i.position, i.age].filter(known).join(" · ");
  const detail = [
    meta && `<p>📍 ${esc(meta)}</p>`,
    known(i.style) && `<p>🎮 ${esc(i.style)}</p>`,
    known(i.fit) && `<p>🧩 ${esc(i.fit)}</p>`,
    `<p class="src">${known(i.source) ? "🗞 " + esc(i.source) : ""}${known(i.source) && i.outlet ? " · " : ""}${esc(i.outlet || "")}` +
      `${i.url ? ` · <a href="${esc(i.url)}" target="_blank" rel="noopener">Read more</a>` : ""}</p>`,
  ].filter(Boolean).join("");
  return `<div class="card">
    <div class="head">${avatarHTML(mainClub)}
      <div class="who"><div class="player">${esc(i.player)}</div><div class="move">${move}</div></div>
      ${stageBadge(i)}${inout}</div>
    ${known(i.fee) ? `<div class="fee">💰 ${esc(i.fee)}</div>` : ""}
    <div class="morelink">More ›</div>
    <div class="detail">${detail}</div>
  </div>`;
}

function renderCards(items, container, forClub) {
  let html = "", lastDay = "";
  for (const i of items) {
    const day = dayLabel(i.ts);
    if (day !== lastDay) { html += `<div class="day">${day}</div>`; lastDay = day; }
    html += cardHTML(i, forClub);
  }
  container.innerHTML = html;
}

// expand/collapse via delegation; links inside cards still work
document.addEventListener("click", (e) => {
  if (e.target.closest("a")) return;
  const card = e.target.closest(".card");
  if (card) card.classList.toggle("open");
});

// ---------- feed ----------
async function loadFeed() {
  try {
    // unique query defeats the Pages CDN cache — cards show as soon as
    // the bot commits them
    const r = await fetch(`feed.json?t=${Date.now()}`, { cache: "no-cache" });
    feed = await r.json();
  } catch {
    feed = [];
  }
  renderFeed();
}

const STAGE_CHIPS = [
  ["", "All"],
  ["rumour", "👀 Rumours"],
  ["Here we go", "🚦 Here we go"],
  ["Medical", "🩺 Medical"],
  ["Completed", "✅ Completed"],
];
let feedStage = "";

function renderFeedChips() {
  $("#feed-chips").innerHTML = STAGE_CHIPS.map(
    ([v, label]) => `<button class="chip${v === feedStage ? " active" : ""}" data-stage="${v}">${label}</button>`
  ).join("");
  $("#feed-chips").querySelectorAll(".chip").forEach((ch) =>
    ch.addEventListener("click", (e) => {
      e.stopPropagation();
      feedStage = ch.dataset.stage;
      renderFeedChips();
      renderFeed();
    })
  );
}

function renderFeed() {
  const items = feed.filter((i) => matchesStage(i, feedStage));
  renderCards(items, $("#cards"));
  $("#feed-empty").hidden = items.length > 0;
}

// ---------- clubs ----------
function renderClubs() {
  $("#club-detail").hidden = true;
  const groups = new Map();
  feed.forEach((i) =>
    clubsOf(i).forEach((c) => {
      if (!groups.has(c)) groups.set(c, []);
      groups.get(c).push(i);
    })
  );
  const rows = [...groups.entries()]
    .sort((a, b) => b[1].length - a[1].length || a[0].localeCompare(b[0]))
    .map(([club, items]) => {
      const deals = items.filter((i) => i.kind !== "interest").length;
      const rumours = items.length - deals;
      const sub = `${deals} deal${deals === 1 ? "" : "s"}` + (rumours ? ` · ${rumours} rumour${rumours === 1 ? "" : "s"}` : "");
      return `<div class="player-row" data-club="${esc(club)}">${avatarHTML(club)}
        <div class="grow"><div class="n">${esc(club)}</div><div class="sub">${sub}</div></div>
        <div class="chev">›</div></div>`;
    });
  $("#club-list").innerHTML = rows.join("") || '<p class="empty">No clubs yet.</p>';
  $("#club-list").querySelectorAll(".player-row").forEach((el) =>
    el.addEventListener("click", () => openClub(el.dataset.club))
  );
}

let clubStage = "";

function openClub(club) {
  document.querySelector('nav button[data-tab="clubs"]').click();
  clubStage = "";
  $("#club-list").innerHTML = "";
  $("#club-detail").hidden = false;
  $("#club-title").textContent = club;
  $("#club-chips").innerHTML = STAGE_CHIPS.map(
    ([v, label]) => `<button class="chip${v === "" ? " active" : ""}" data-stage="${v}">${label}</button>`
  ).join("");
  $("#club-chips").querySelectorAll(".chip").forEach((ch) =>
    ch.addEventListener("click", (e) => {
      e.stopPropagation();
      clubStage = ch.dataset.stage;
      $("#club-chips").querySelectorAll(".chip").forEach((x) => x.classList.toggle("active", x === ch));
      renderClubCards(club);
    })
  );
  renderClubCards(club);
}

function renderClubCards(club) {
  const items = feed.filter((i) => clubsOf(i).includes(club) && matchesStage(i, clubStage));
  if (items.length) renderCards(items, $("#club-cards"), club);
  else $("#club-cards").innerHTML = '<p class="empty">Nothing here yet.</p>';
}

$("#club-back").addEventListener("click", renderClubs);

// ---------- push notifications ----------
async function enablePush() {
  const status = $("#push-status");
  try {
    if (!("serviceWorker" in navigator) || !("PushManager" in window)) {
      status.textContent = "Push isn't supported here. On iPhone: install via Share → Add to Home Screen, then open the installed app.";
      return;
    }
    const perm = await Notification.requestPermission();
    if (perm !== "granted") {
      status.textContent = "Permission denied — enable notifications for this app in iOS Settings.";
      return;
    }
    const reg = await navigator.serviceWorker.ready;
    const sub = await reg.pushManager.subscribe({
      userVisibleOnly: true,
      applicationServerKey: urlB64(VAPID_PUBLIC_KEY),
    });
    $("#push-sub").value = JSON.stringify(sub.toJSON());
    $("#push-result").hidden = false;
    status.textContent = "Subscribed on this device ✓";
  } catch (e) {
    status.textContent = "Failed: " + e.message;
  }
}
$("#push-btn").addEventListener("click", enablePush);
$("#push-copy").addEventListener("click", async () => {
  await navigator.clipboard.writeText($("#push-sub").value);
  $("#push-copy").textContent = "Copied ✓";
});

function urlB64(s) {
  const pad = "=".repeat((4 - (s.length % 4)) % 4);
  const raw = atob((s + pad).replace(/-/g, "+").replace(/_/g, "/"));
  return Uint8Array.from([...raw].map((c) => c.charCodeAt(0)));
}

// ---------- push subscription health ----------
async function checkPushHealth() {
  try {
    if (!("serviceWorker" in navigator) || !("PushManager" in window)) return;
    if (Notification.permission !== "granted") return;
    const reg = await navigator.serviceWorker.ready;
    const sub = await reg.pushManager.getSubscription();
    if (!sub) return;
    const r = await fetch(`push-meta.json?t=${Date.now()}`, { cache: "no-cache" });
    if (!r.ok) return;
    const meta = await r.json();
    const buf = await crypto.subtle.digest("SHA-256", new TextEncoder().encode(sub.endpoint));
    const hex = [...new Uint8Array(buf)].map((b) => b.toString(16).padStart(2, "0")).join("").slice(0, 16);
    if (!meta.endpoints.includes(hex)) {
      $("#push-status").textContent =
        "⚠️ Notifications are broken: this device's subscription is no longer paired. " +
        "Tap Enable notifications, copy the code, and send it to @Kahab_bot on Telegram.";
      document.querySelector('nav button[data-tab="settings"]').classList.add("attention");
    }
  } catch { /* diagnostics must never break the app */ }
}

// ---------- boot ----------
renderFeedChips();
let swReg = null;
if ("serviceWorker" in navigator) {
  navigator.serviceWorker.register("sw.js").then((reg) => { swReg = reg; });
  navigator.serviceWorker.addEventListener("message", (e) => {
    if (e.data === "refresh-feed") loadFeed();
  });
  // Auto-update: when a new service worker takes control, reload once so
  // the fresh version applies immediately — no more being one open behind.
  let hadController = !!navigator.serviceWorker.controller;
  navigator.serviceWorker.addEventListener("controllerchange", () => {
    if (!hadController) { hadController = true; return; } // first-ever install
    location.reload();
  });
}
document.addEventListener("visibilitychange", () => {
  if (!document.hidden) {
    loadFeed();
    if (swReg) swReg.update().catch(() => {}); // check for app updates on focus
    if (navigator.clearAppBadge) navigator.clearAppBadge().catch(() => {});
  }
});
if (navigator.clearAppBadge) navigator.clearAppBadge().catch(() => {});
checkPushHealth();
loadFeed().then(() => {
  if (location.hash === "#clubs") document.querySelector('nav button[data-tab="clubs"]').click();
  else if (location.hash.startsWith("#club=")) openClub(decodeURIComponent(location.hash.slice(6)));
});
setInterval(loadFeed, 5 * 60 * 1000);
$("#version").textContent = "ShimShim v3.2";
