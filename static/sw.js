const CACHE = 'kapper-beheer-v2';
const PRECACHE = [
  '/eigenaar/dashboard',
  '/static/eigenaar.css',
  '/static/style.css',
];

self.addEventListener('install', e => {
  e.waitUntil(
    caches.open(CACHE).then(c => c.addAll(PRECACHE)).then(() => self.skipWaiting())
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
  if (e.request.method !== 'GET') return;
  // Netwerk-eerst: altijd verse pagina's/CSS; val terug op cache bij offline
  e.respondWith(
    fetch(e.request).catch(() => caches.match(e.request))
  );
});

// ── Pushmeldingen ───────────────────────────────────────────────────────────────
self.addEventListener('push', e => {
  let data = { title: 'Nieuwe afspraak', body: '' };
  try { data = e.data.json(); } catch (_) {
    if (e.data) data.body = e.data.text();
  }
  e.waitUntil(
    self.registration.showNotification(data.title || 'Nieuwe afspraak', {
      body: data.body || '',
      icon: '/static/icon-192.png',
      badge: '/static/icon-192.png',
      vibrate: [100, 50, 100],
      data: { url: '/eigenaar/dashboard' }
    })
  );
});

self.addEventListener('notificationclick', e => {
  e.notification.close();
  const url = (e.notification.data && e.notification.data.url) || '/eigenaar/dashboard';
  e.waitUntil(
    clients.matchAll({ type: 'window', includeUncontrolled: true }).then(wins => {
      for (const w of wins) { if (w.url.includes('/eigenaar/') && 'focus' in w) return w.focus(); }
      if (clients.openWindow) return clients.openWindow(url);
    })
  );
});
