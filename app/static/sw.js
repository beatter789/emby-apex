// Static assets only.  Authentication HTML and every /api/v1 response stay
// network-bound so cached responses cannot cross session boundaries.
const CACHE_NAME = 'emby-apex-ui-v15';
// Historical marker retained for older repository checks; v15 is the active cache.
// emby-apex-ui-v12
// The previous mount-DSRKV7aU.js build is intentionally not cached; only the
// current hash below is eligible for static caching.
const STATIC_ASSETS = [
  '/static/ui.css',
  '/static/app.js',
  '/static/logoicon.png',
  '/static/brand-logo.png',
  '/manifest.webmanifest',
  '/static/frontend/admin.js',
  '/static/frontend/portal.js',
  '/static/frontend/chunks/mount-CFIGgn9i.js',
  '/static/frontend/assets/mount.css',
  '/static/frontend/assets/portal.css',
];

self.addEventListener('install', (event) => {
  event.waitUntil(caches.open(CACHE_NAME).then((cache) => cache.addAll(STATIC_ASSETS)).then(() => self.skipWaiting()));
});
self.addEventListener('activate', (event) => {
  event.waitUntil(caches.keys().then((keys) => Promise.all(keys.filter((key) => key !== CACHE_NAME).map((key) => caches.delete(key)))).then(() => self.clients.claim()));
});
self.addEventListener('fetch', (event) => {
  const request = event.request;
  const url = new URL(request.url);
  if (request.method !== 'GET' || url.origin !== self.location.origin) return;
  const isApi = url.pathname === '/api/v1' || url.pathname.startsWith('/api/v1/');
  const isHtml = request.mode === 'navigate' || request.destination === 'document'
    || request.headers.get('accept')?.toLowerCase().includes('text/html');
  // Navigation responses and API JSON are account-bound. Never cache them,
  // even when a browser omits the usual document destination hint.
  if (isApi || isHtml) return;
  const isStatic = (url.pathname.startsWith('/static/') && (
    request.destination === 'style' || request.destination === 'script' || request.destination === 'image' || request.destination === 'font'
  )) || url.pathname.endsWith('.webmanifest');
  // Never put navigations or API responses in Cache Storage. Their responses
  // may contain account-specific HTML, CSRF tokens, or private request data,
  // and Cache API keys do not vary by session cookie.
  if (!isStatic) return;
  event.respondWith(
    fetch(request).then((response) => {
      if (response.ok) caches.open(CACHE_NAME).then((cache) => cache.put(request, response.clone()));
      return response;
    }).catch(() => caches.match(request))
  );
});
