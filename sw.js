// 轻量安装态：只让浏览器认得“可安装”，不做任何缓存。
// 每次请求都直接走网络，避免“改了内容用户还看到旧版”。
self.addEventListener('install', function () {
  self.skipWaiting();
});

self.addEventListener('activate', function (event) {
  event.waitUntil(self.clients.claim());
});

self.addEventListener('fetch', function (event) {
  event.respondWith(fetch(event.request));
});
