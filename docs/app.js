const VAPID_PUBLIC_KEY = "BDKwnexe_jAsbln6CFqhe9qMnjyh3tsOsIW5YcV9UN39-E7kjRjHGJsJAnhkT4k8Z8pCm5edQnGrNX8Icx4WENM";
const $ = (s) => document.querySelector(s);
let feed = [];

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

function ago(ts) {
  const m = Math.max(0, (Date.now() - new Date(ts)) / 60000);
  if (m < 60) return Math.round(m) + "m";
  if (m < 60 * 24) return Math.round(m / 60) + "h";
  return Math.round(m / 60 / 24) + "d";
}

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

// All clubs an item involves: destination(s) + origin.
function clubsOf(i) {
  const out = new Set();
  if (known(i.to_club)) i.to_club.split(",").forEach((c) => known(c) && out.add(canonClub(c)));
  if (known(i.from_club)) out.add(canonClub(i.from_club));
  return [...out];
}

// Unified stage of an item: "rumour" for the interest track, else its deal stage.
const stageOf = (i) => (i.kind === "interest" ? "rumour" : known(i.stage) ? i.stage : "Completed");

// ---------- cards ----------
function cardHTML(i, forClub) {
  const isRumour = i.kind === "interest";
  const badge = isRumour
    ? '<span class="badge interest">👀 Rumour</span>'
    : `<span class="badge deal">${esc(stageOf(i))}</span>`;
  let tag = "";
  if (forClub && !isRumour) {
    const joined = known(i.to_club) && canonClub(i.to_club) === forClub;
    tag = `<span class="badge ${joined ? "in" : "out"}">${joined ? "⬅ In" : "➡ Out"}</span>`;
  }
  const meta = [i.position, i.age].filter(known).join(" · ");
  const move = isRumour
    ? `${known(i.from_club) ? "🏟 " + esc(i.from_club) + " · " : ""}🎯 ${esc(i.to_club)}`
    : `🔄 ${esc(i.from_club)} → ${esc(i.to_club)}`;
  return `<div class="card">
    <div class="top"><span class="player">${esc(i.player)}</span>
      <span class="when">${ago(i.ts)} ago</span></div>
    ${meta ? `<div class="meta">📍 ${esc(meta)}</div>` : ""}
    <div class="move">${move}${badge}${tag}</div>
    ${known(i.fee) ? `<div class="line"><b>💰 Fee:</b> ${esc(i.fee)}</div>` : ""}
    ${known(i.style) ? `<div class="line"><b>🎮 Style:</b> ${esc(i.style)}</div>` : ""}
    ${known(i.fit) ? `<div class="line"><b>🧩 Fit:</b> ${esc(i.fit)}</div>` : ""}
    <div class="foot">${known(i.source) ? "🗞 " + esc(i.source) + " · " : ""}${esc(i.outlet || "")}
      ${i.url ? ` · <a href="${esc(i.url)}" target="_blank" rel="noopener">Read more</a>` : ""}</div>
  </div>`;
}

// ---------- feed ----------
async function loadFeed() {
  try {
    // unique query defeats the Pages CDN cache (~10 min) — cards show the
    // moment the bot commits them
    const r = await fetch(`feed.json?t=${Date.now()}`, { cache: "no-cache" });
    feed = await r.json();
  } catch {
    feed = [];
  }
  renderFeed();
}

function matchesStage(i, want) {
  if (!want) return true;
  return stageOf(i) === want;
}

// The feed is filtered by stage only — chips shared with the club pages.
let feedStage = "";

function renderFeedChips() {
  $("#feed-chips").innerHTML = STAGE_CHIPS.map(
    ([v, label]) => `<button class="chip${v === feedStage ? " active" : ""}" data-stage="${v}">${label}</button>`
  ).join("");
  $("#feed-chips").querySelectorAll(".chip").forEach((ch) =>
    ch.addEventListener("click", () => {
      feedStage = ch.dataset.stage;
      renderFeedChips();
      renderFeed();
    })
  );
}

function renderFeed() {
  const items = feed.filter((i) => matchesStage(i, feedStage));
  $("#cards").innerHTML = items.map((i) => cardHTML(i)).join("");
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
      return `<div class="player-row" data-club="${esc(club)}">
        <div><div class="n">${esc(club)}</div><div class="sub">${sub}</div></div>
        <div>›</div></div>`;
    });
  $("#club-list").innerHTML = rows.join("") || '<p class="empty">No clubs yet.</p>';
  $("#club-list").querySelectorAll(".player-row").forEach((el) =>
    el.addEventListener("click", () => openClub(el.dataset.club))
  );
}

const STAGE_CHIPS = [
  ["", "All"],
  ["rumour", "👀 Rumours"],
  ["Here we go", "🚦 Here we go"],
  ["Medical", "🩺 Medical"],
  ["Completed", "✅ Completed"],
];
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
    ch.addEventListener("click", () => {
      clubStage = ch.dataset.stage;
      $("#club-chips").querySelectorAll(".chip").forEach((x) => x.classList.toggle("active", x === ch));
      renderClubCards(club);
    })
  );
  renderClubCards(club);
}

function renderClubCards(club) {
  const items = feed.filter((i) => clubsOf(i).includes(club) && matchesStage(i, clubStage));
  $("#club-cards").innerHTML =
    items.map((i) => cardHTML(i, club)).join("") || '<p class="empty">Nothing here yet.</p>';
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
// iOS occasionally rotates the push subscription; the paired one then goes
// stale and pushes vanish silently. Compare our endpoint's hash against the
// published pairing list and warn instead of staying quiet.
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
        "Tap Enable notifications and send the new code to Claude.";
      document.querySelector('nav button[data-tab="settings"]').classList.add("attention");
    }
  } catch { /* diagnostics must never break the app */ }
}

// ---------- boot ----------
renderFeedChips();
if ("serviceWorker" in navigator) {
  navigator.serviceWorker.register("sw.js");
  // a push arrived while the app is open — show the new card immediately
  navigator.serviceWorker.addEventListener("message", (e) => {
    if (e.data === "refresh-feed") loadFeed();
  });
}
// refresh whenever the app comes back to the foreground
document.addEventListener("visibilitychange", () => {
  if (!document.hidden) {
    loadFeed();
    if (navigator.clearAppBadge) navigator.clearAppBadge().catch(() => {});
  }
});
if (navigator.clearAppBadge) navigator.clearAppBadge().catch(() => {});
checkPushHealth();
loadFeed().then(() => {
  if (location.hash === "#clubs") document.querySelector('nav button[data-tab="clubs"]').click();
  else if (location.hash.startsWith("#club=")) openClub(decodeURIComponent(location.hash.slice(6)));
});
setInterval(loadFeed, 5 * 60 * 1000); // refresh while open
$("#version").textContent = "ShimShim v2.5";
