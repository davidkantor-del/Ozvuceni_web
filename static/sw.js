const CACHE_NAME = "ozvuceni-cache-v1";
const URLS_TO_CACHE = [
  "/",
  "/static/manifest.json",
  "/static/style.css",
  "/static/app.js",
  "/static/logo.png"
];

// Install: přednačti základní soubory
self.addEventListener("install", (event) => {
  event.waitUntil(
    caches.open(CACHE_NAME).then((cache) => cache.addAll(URLS_TO_CACHE))
  );
});

// Activate: cleanup starých cache (pokud změníme verzi)
self.addEventListener("activate", (event) => {
  event.waitUntil(
    caches.keys().then((keys) =>
      Promise.all(keys.map((k) => (k === CACHE_NAME ? null : caches.delete(k))))
    )
  );
});

// Fetch: síť -> cache fallback (nejjednodušší strategie)
self.addEventListener("fetch", (event) => {
  const req = event.request;

  // Socket.IO a API volání přes síť, necacheovat
  const isSocket = req.url.includes("/socket.io/");
  if (isSocket) return;

  event.respondWith(
    fetch(req).then((res) => {
      // úspěšná odpověď – aktualizuj cache
      const resClone = res.clone();
      caches.open(CACHE_NAME).then((cache) => cache.put(req, resClone)).catch(()=>{});
      return res;
    }).catch(() =>
      caches.match(req).then((cached) => cached || caches.match("/"))
    )
  );
});
