const CACHE_NAME="hesabpak-static-v3";
const ROOT = new URL("./", self.location).pathname;
const ASSETS=[
  ROOT+"static/app.js",ROOT+"static/assistant.js",ROOT+"static/rates.js",ROOT+"static/sales.js",
  ROOT+"static/search-ajax.js",ROOT+"static/search-unified.js",
  ROOT+"static/style.css",ROOT+"static/sales.css",ROOT+"static/search.css",
  ROOT+"static/paki.css",ROOT+"static/paki.js",ROOT+"static/favicon.svg",
  ROOT+"static/manifest.webmanifest",ROOT+"static/icons/paki.svg"
];
self.addEventListener("install",event=>{
  event.waitUntil(caches.open(CACHE_NAME).then(cache=>cache.addAll(ASSETS)).then(()=>self.skipWaiting()));
});
self.addEventListener("activate",event=>{
  event.waitUntil(caches.keys().then(keys=>Promise.all(
    keys.filter(key=>key!==CACHE_NAME).map(key=>caches.delete(key))
  )).then(()=>self.clients.claim()));
});
self.addEventListener("fetch",event=>{
  const request=event.request;
  if(request.method!=="GET") return;
  const url=new URL(request.url);
  if(url.origin!==self.location.origin) return;
  if(!url.pathname.includes("/static/")) return;
  event.respondWith(
    caches.match(request).then(cached=>cached||fetch(request).then(response=>{
      if(response.ok){
        const copy=response.clone();
        caches.open(CACHE_NAME).then(cache=>cache.put(request,copy)).catch(()=>{});
      }
      return response;
    }).catch(()=>caches.match(request)))
  );
});
