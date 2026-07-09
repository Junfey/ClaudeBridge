// ClaudeBridge service worker: enables PWA install + push notifications.
const CACHE = 'cb-v1';
// The app icon INLINED as a data URI. A notification's icon is otherwise fetched
// from the SW's origin — and if the phone subscribed on a since-dead random
// tunnel (e.g. wicked-deer-xxx.loca.lt), that fetch fails and Chrome draws a
// letter avatar ("W"). Embedding it makes the icon always render.
const ICON = 'data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAAAMAAAADACAYAAABS3GwHAAAGIElEQVR4nO3dUXLbRhCEYTqVswBHsm/iHCG+SXwk72WcUimKZRZFEovZ3Z7p/3vyg0tcgN2zAElRlwsAAAAAAAAAAAAAAEAZny6J/fj6+efqNeDV/u17yiylWTRhz2dPUArpBRL6OnbRMkguiuDXtYsVQWoxBN/HLlIEiUUQfF/74iIsfXCCj9VF+OOyCOGHQh6mt47gQ2k3mLoDEH6o5WRaAQg/FPMypQCEH6q5GV4Awg/l/AwtAOGHeo6GFYDwI0OehhSA8CNLrsILQPgxUnS+QgtA+DFDZM6WfRQCUBBWAKY/ZorKW0gBCD9WiMgdl0CwdroATH+sdDZ/7ACwdqoATH8oOJNDdgBY6y4A0x9KevPIDrDY9vc/q5dgrasATH8o6sklOwCsUQCByx8ugxIVgMsfKDuaT3YAWKMAsEYBFuG6XwMFEEEhEhSAG2BkcCSn7ACwRgEW4HJHBwUQQjHmowCwRgFgjQJMxmWOFgoAa6ULkHHaqq15E1tPtD8vhbW/vvz/BL78G8/bTM5b6R3AaZJF2ozOVfkCvJ9gL0/syidXPVjb1fmpPv0tCnDriVQP4grb1TlxCL9NAW5ZvRvcM3Ndm/B5mMGmAB9NNOcnf/vg2F2mv1UBHpVgdBGUinbveJtR+O0KkCmkozgc4xF2BXg04apeEz9zXM1s+lsW4NknenUJIh//mZ/VDMNf/p1glXdDV5VpdYkzsNwBjoY6Y5COrLmZTn/rAvSUIEMRjq6zGYf/4l6ADG9Sjfz/oABdE/DIlJ0Ryt7dqZlP/xfsACeCoDBxe9dA+F/xKlDSz80rlK8CdoD/ZHqp8+xjMf1/oQADr8UjSnH985j8sSjAgMk4IqRRP5Pp/zsKkKAEhH8cCgBrFMDkMqHiMUWgAAaBqXQs0SgArFGA4pOzwjGMRAEKByjz2mehALBGAYpO0oxrXoECFAxUprWuRgFgjQIUm6wZ1qiE3wd4INunL12/5LYXBUge+EcoxH3WBagW9t5jbsa7hFUBHAP/jM34sql0AQh8zHlrhQtRqgAEfs55bYUKkbYAhF3r3LekpUhTAAKvbUu6S8gWgMDntiUphGwBbp0wSpFDEw17qgLcQin0tERhT1+AW/gbwGvPd3bpC3CNXWLsuaymXAFuoRR958iBRQFucS6Fa9hvsS2A0/0Egf8YBSi2SxD2Y/iNsAPUw59ljUooAKxRgIKTNdNaV6MARQOVcc0rUABYowCFJ2nmtc9CAYoHqMIxjEQBYI0CGEzOSscSjQKYBKbiMUWgALBGARL8TV7Fv11cBQUQD3/0z6QEv6MAwSF9H9SI0F7/PD7tGYsCBE3GmcE8+1jsAr/w+wAnrZrIb49LmM9hBzgRIoXLkd41UJxX9gXoCcKRa/EZJem9N9h4VYgC9IRN9bEUdqRsrHeAIxMwyyswR9e5me8CtgU4Gv5sKMFzbAswc+qvfKUoY3lnsizAM9N/dXBmf5RiM70UsivAoye66tR85rg2wxLYFeCeisF3PMYjrArw0YSbMfWVgnfveDezXcCmAPfC76pRAt/PAikHf8WbbZvZ5LfaAbL8wbaVWtFvxr64F+D9E7n6FR714rWr8+NQgvIFyBI+Jc3oXJW+B3iZYE5P5qh7g1b4HJbeATI+cWprbmLriVa6AMAjFGCy6hM1GwoAaxQA1iiAEC6P5qMACxB0HRQA1igArB0qwP7t+6dxS/HGZVGcIzllB1iEwGugALBGAWCNAsDa4QJwIxx/H8D9QJyj+WQHgDUKAGvdr+v/+Pr5Z+xSgHN6Ls/ZAWCtuwDcDENJbx7ZAWDtVAHYBaDgTA7ZAWDtdAHYBbDS2fyxA8BaSAHYBbBCRO7CdgBKgJmi8sYlEKyFFoBdADNE5ix8B6AEGCk6X0MugSgBsuRq2D0AJUCGPA09CaYEUM/R8FeBKAGU8zPlZVBKANXcTHsfgBJAMS9T3wijBFDLybLv+uR3iqEwIJd9FILdAAp5kPi2Z3YDX/vibxyXKMAbiuBjF/mqfYlFXKMIde0iwX8jtZhrFKGOXSz4byQXdQtlyGcXDf178gu8h1Lo2BOEHQAAAAAAAAAAAABwMfAvIyeE39W5hcYAAAAASUVORK5CYII=';

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
    icon: ICON,
    badge: ICON,
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
      body: button, tag: 'cb-ack', icon: ICON, badge: ICON, silent: true,
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
