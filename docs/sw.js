/* ClipIn phone offline copy: the app itself opens instantly and offline; GitHub (runs, clips) always goes
   to the network. Bump VERSION when the app files change so phones pick up the new copy. */
const VERSION = '1.3.0';
const CACHE = 'clipin-' + VERSION;
const CAPS = ['bold_pop', 'two_tone', 'one_word', 'neon', 'fire', 'ice', 'rainbow', 'pastel', 'karaoke', 'boxed',
  'typewriter', 'clean', 'subtitle'].map(n => `captions/${n}.png`);
const FILES = ['./', 'index.html', 'app.css', 'app.js', 'manifest.webmanifest',
  'icons/icon-180.png', 'icons/icon-192.png', 'icons/icon-512.png', ...CAPS];

self.addEventListener('install', e => {
  e.waitUntil(caches.open(CACHE).then(c => c.addAll(FILES)).then(() => self.skipWaiting()));
});
self.addEventListener('activate', e => {
  e.waitUntil(caches.keys().then(keys => Promise.all(keys.filter(k => k.startsWith('clipin-') && k !== CACHE)
    .map(k => caches.delete(k)))).then(() => self.clients.claim()));
});
self.addEventListener('fetch', e => {
  const url = new URL(e.request.url);
  if (e.request.method !== 'GET' || url.origin !== location.origin) return;   // GitHub API and clips: network
  e.respondWith((async () => {
    const cache = await caches.open(CACHE);
    const key = e.request.mode === 'navigate' ? 'index.html' : e.request;
    const hit = await cache.match(key, { ignoreSearch: true });
    const fresh = fetch(e.request).then(r => { if (r.ok) cache.put(key, r.clone()); return r; }).catch(() => null);
    if (hit) { e.waitUntil(fresh); return hit; }
    return (await fresh) || new Response('ClipIn is offline and this file was not saved yet.', { status: 503 });
  })());
});
