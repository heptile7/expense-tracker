// Service worker for the home-screen app. Served at /sw.js so it covers the whole site.
//
// What it does, and deliberately doesn't:
//   * caches only an offline notice, never the app's data: statements, balances and payments
//     stay on the computer running the app, not on the phone;
//   * receives statements shared from other apps (WhatsApp, Gmail, Files) through the share
//     sheet, keeps them just long enough for the page to upload them, then the page deletes them.
"use strict";

const SHELL = "expenses-shell-v1";
const SHARED = "expenses-shared";

self.addEventListener("install", (event) => {
  event.waitUntil(caches.open(SHELL).then((c) => c.addAll(["/offline.html", "/static/icons/icon-192.png"])).then(() => self.skipWaiting()));
});

self.addEventListener("activate", (event) => {
  event.waitUntil(
    caches.keys()
      .then((keys) => Promise.all(keys.filter((k) => k !== SHELL && k !== SHARED).map((k) => caches.delete(k))))
      .then(() => self.clients.claim()),
  );
});

self.addEventListener("fetch", (event) => {
  const url = new URL(event.request.url);
  if (url.origin !== self.location.origin) return;
  if (event.request.method === "POST" && url.pathname === "/share") {
    event.respondWith(receiveShare(event.request));
    return;
  }
  if (event.request.mode === "navigate") {
    event.respondWith(fetch(event.request).catch(() => caches.match("/offline.html")));
    return;
  }
  if (url.pathname === "/static/icons/icon-192.png") {
    event.respondWith(fetch(event.request).catch(() => caches.match(event.request)));
  }
  // Everything else goes straight to the network, uncached.
});

async function receiveShare(request) {
  const form = await request.formData();
  const files = form.getAll("file").filter((f) => f && typeof f === "object" && f.name);
  const cache = await caches.open(SHARED);
  for (const key of await cache.keys()) await cache.delete(key);
  await Promise.all(files.slice(0, 5).map((f, i) => cache.put(`/shared/${i}`, new Response(f, {
    headers: { "Content-Type": f.type || "application/octet-stream", "X-Filename": encodeURIComponent(f.name) },
  }))));
  return Response.redirect(`/?shared=${files.length}#import`, 303);
}
