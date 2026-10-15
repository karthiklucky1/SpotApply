// SpotApply Service Worker — offline shell + asset cache
// v2 (2026-10): cache our own static files only — never an HTML page, so a
// signed-in page (dashboard, application views) can never outlive sign-out in
// the browser's cache. Bumping the name deletes the v1 cache on activate.
const CACHE = 'spotapply-v2';
const SHELL = ['/login', '/'];

self.addEventListener('install', e => {
  e.waitUntil(
    caches.open(CACHE).then(c => c.addAll(SHELL)).then(() => self.skipWaiting())
  );
});

self.addEventListener('activate', e => {
  e.waitUntil(
    caches.keys().then(keys =>
      Promise.all(keys.filter(k => k !== CACHE).map(k => caches.delete(k)))
    ).then(() => self.clients.claim())
  );
});

self.addEventListener('fetch', e => {
  // Only handle GET requests to same origin
  if (e.request.method !== 'GET' || !e.request.url.startsWith(self.location.origin)) return;

  // API calls — network only, no cache
  if (e.request.url.includes('/api/')) return;

  e.respondWith(
    fetch(e.request)
      .then(res => {
        // Cache our static assets only (styles, scripts, fonts, icons)
        if (res.ok && new URL(e.request.url).pathname.startsWith('/static/')) {
          const clone = res.clone();
          caches.open(CACHE).then(c => c.put(e.request, clone));
        }
        return res;
      })
      .catch(() => caches.match(e.request).then(r => r || caches.match('/')))
  );
});
