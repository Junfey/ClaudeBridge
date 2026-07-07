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

// Push from the bridge → show a notification. May carry answer buttons (actions)
// and the chat text preview in `body`.
self.addEventListener('push', (e) => {
  let data = { title: 'ClaudeBridge', body: 'Есть обновление', tag: 'cb', url: '/' };
  try { if (e.data) data = Object.assign(data, e.data.json()); } catch (_) {}
  const opts = {
    body: data.body,
    tag: data.tag,
    icon: '/static/icon-192.png',
    badge: '/static/icon-192.png',
    // stash everything needed to answer straight from the notification
    data: { url: data.url || '/', target_id: data.target_id || '', opts: data.opts || null },
    vibrate: [120, 60, 120, 60, 120],
    renotify: true,
    requireInteraction: true,   // Chrome/Android: stay on screen until tapped (Safari ignores)
    silent: false,
  };
  // Action buttons (Chrome/Android only — Safari/iOS ignores `actions` safely).
  if (Array.isArray(data.actions) && data.actions.length) opts.actions = data.actions;
  e.waitUntil((async () => {
    await self.registration.showNotification(data.title, opts);
    // App-icon count badge = number of pending notifications (unread chats).
    try {
      const ns = await self.registration.getNotifications();
      const n = ns.filter(x => x.tag !== 'cb-ack').length || 1;
      if (self.navigator.setAppBadge) await self.navigator.setAppBadge(n);
    } catch (_) {}
  })());
});

// Read the bridge auth key that the page stashed in the Cache (so the SW can
// call the API without the page being open).
async function readKey() {
  try {
    const c = await caches.open('cb-key');
    const r = await c.match('/__key');
    return r ? (await r.text()) : '';
  } catch (_) { return ''; }
}

// Answer a question/permission from a notification action button.
async function answerFromPush(targetId, button) {
  try {
    const key = await readKey();
    const r = await fetch('/api/cdp/answer-push', {
      method: 'POST',
      headers: { 'content-type': 'application/json', 'x-bridge-key': key },
      body: JSON.stringify({ target_id: targetId, button }),
    });
    await self.registration.showNotification(r.ok ? 'Ответ отправлен ✓' : 'Не удалось отправить', {
      body: button, tag: 'cb-ack', icon: '/static/icon-192.png', badge: '/static/icon-192.png', silent: true,
    });
  } catch (_) {
    // No connection → open the chat so the user can answer in-app.
    if (self.clients.openWindow) self.clients.openWindow('/?open=' + targetId);
  }
}

// Tapping a notification → answer (if an action button) or open the chat.
self.addEventListener('notificationclick', (e) => {
  const nd = e.notification.data || {};
  const action = e.action || '';
  e.notification.close();
  if (action.indexOf('opt:') === 0 && nd.opts && nd.opts[action]) {
    e.waitUntil(answerFromPush(nd.target_id, nd.opts[action]));
    return;
  }
  const url = nd.url || '/';
  e.waitUntil((async () => {
    try { if (self.navigator.clearAppBadge) await self.navigator.clearAppBadge(); } catch (_) {}
    const list = await self.clients.matchAll({ type: 'window', includeUncontrolled: true });
    for (const c of list) { if ('focus' in c) { c.navigate(url); return c.focus(); } }
    if (self.clients.openWindow) return self.clients.openWindow(url);
  })());
});
