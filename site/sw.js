// App instalável: busca sempre a versão mais nova na rede e guarda uma cópia para abrir sem internet.
const CACHE = 'og-brasil-v1';
self.addEventListener('install', e => self.skipWaiting());
self.addEventListener('activate', e => e.waitUntil(
  caches.keys().then(ks => Promise.all(ks.filter(k => k !== CACHE).map(k => caches.delete(k)))).then(() => self.clients.claim())));
self.addEventListener('fetch', e => {
  const req = e.request;
  if (req.method !== 'GET') return;
  const url = new URL(req.url);
  const chave = url.origin === location.origin ? url.origin + url.pathname : req.url;   // ignora ?v= da data
  e.respondWith(fetch(req).then(res => {
    if (res.ok && (url.origin === location.origin || res.type === 'cors')) {
      const copia = res.clone(); caches.open(CACHE).then(c => c.put(chave, copia));
    }
    return res;
  }).catch(() => caches.match(chave).then(r => r || caches.match(req))));
});
