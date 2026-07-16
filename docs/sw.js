const CACHE = "shimshim-v10";
const SHELL = ["./", "index.html", "style.css", "app.js", "manifest.webmanifest", "icon-192.png", "icon-512.png"];

self.addEventListener("install", (e) => {
  e.waitUntil(caches.open(CACHE).then((c) => c.addAll(SHELL)).then(() => self.skipWaiting()));
});

self.addEventListener("activate", (e) => {
  e.waitUntil(
    caches.keys()
      .then((keys) => Promise.all(keys.filter((k) => k !== CACHE).map((k) => caches.delete(k))))
      .then(() => self.clients.claim())
  );
});

self.addEventListener("fetch", (e) => {
  const url = new URL(e.request.url);
  if (url.pathname.endsWith("feed.json")) {
    // network-first: feed must be fresh, cache is the offline fallback
    e.respondWith(
      fetch(e.request)
        .then((r) => {
          const copy = r.clone();
          caches.open(CACHE).then((c) => c.put(e.request, copy));
          return r;
        })
        .catch(() => caches.match(e.request))
    );
  } else if (url.origin === location.origin) {
    e.respondWith(caches.match(e.request).then((hit) => hit || fetch(e.request)));
  }
});

self.addEventListener("push", (e) => {
  let d = {};
  try { d = e.data.json(); } catch { /* empty push */ }
  e.waitUntil((async () => {
    await self.registration.showNotification(d.title || "Transfer news", {
      body: d.body || "",
      icon: "icon-192.png",
      badge: "icon-192.png",
      data: { url: d.url || "./" },
    });
    // if the app is open, refresh its feed right away
    const list = await clients.matchAll({ type: "window" });
    for (const c of list) c.postMessage("refresh-feed");
  })());
});

self.addEventListener("notificationclick", (e) => {
  e.notification.close();
  // open the app: focus it if it's already running, else launch it
  e.waitUntil(
    clients.matchAll({ type: "window", includeUncontrolled: true }).then((list) => {
      for (const c of list) if ("focus" in c) return c.focus();
      return clients.openWindow("./");
    })
  );
});
