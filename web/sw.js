// ClaudeBridge service worker: enables PWA install + push notifications.
const CACHE = 'cb-v1';

self.addEventListener('install', (e) => { self.skipWaiting(); });
self.addEventListener('activate', (e) => { e.waitUntil(self.clients.claim()); });

// Network-first for navigation (content is dynamic); a fetch handler is also
// required for the app to be installable.
self.addEventListener('fetch', (e) => {
  const req = e.request;
  if (req.method !== 'GET') return;
  if (req.mode === 'navigate') {
    e.respondWith(fetch(req).catch(() => caches.match('/')));
    return;
  }
  // cache-first for our own static icons
  if (req.url.includes('/static/icon')) {
    e.respondWith(caches.open(CACHE).then(c => c.match(req).then(hit =>
      hit || fetch(req).then(res => { c.put(req, res.clone()); return res; }))));
  }
});

// Push from the bridge → show a notification.
self.addEventListener('push', (e) => {
  let data = { title: 'ClaudeBridge', body: 'Есть обновление', tag: 'cb', url: '/' };
  try { if (e.data) data = Object.assign(data, e.data.json()); } catch (_) {}
  e.waitUntil(self.registration.showNotification(data.title, {
    body: data.body,
    tag: data.tag,
    icon: '/static/icon-192.png',
    badge: '/static/icon-192.png',
    data: { url: data.url || '/' },
    vibrate: [120, 60, 120, 60, 120],
    renotify: true,
    requireInteraction: true,   // stay on screen / lock screen until tapped
    silent: false,
  }));
});

// Tapping a notification → focus the app (or open it), navigating to the chat.
self.addEventListener('notificationclick', (e) => {
  e.notification.close();
  const url = (e.notification.data && e.notification.data.url) || '/';
  e.waitUntil(self.clients.matchAll({ type: 'window', includeUncontrolled: true }).then(list => {
    for (const c of list) { if ('focus' in c) { c.navigate(url); return c.focus(); } }
    if (self.clients.openWindow) return self.clients.openWindow(url);
  }));
});
