const CACHE_NAME="hesabpak-static-v2";
const ASSETS=[
  "./app.js","./assistant.js","./rates.js","./sales.js",
  "./search-ajax.js","./search-unified.js",
  "./style.css","./sales.css","./search.css","./paki.css","./paki.js",
  "./favicon.svg","./manifest.webmanifest","./icons/paki.svg"
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
  if(url.origin!==self.location.origin || !url.pathname.includes("/static/")) return;
  event.respondWith(
    caches.match(request).then(cached=>cached||fetch(request).then(response=>{
      if(response.ok){
        const copy=response.clone();
        caches.open(CACHE_NAME).then(cache=>cache.put(request,copy)).catch(()=>{});
      }
      return response;
    }))
  );
});
