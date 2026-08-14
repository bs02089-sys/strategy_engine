/* 스윙 투자 알리미 — 서비스 워커 (PWA 설치용)
 *
 * Chrome이 '앱 설치' 메뉴를 표시하려면 서비스 워커가 필요합니다.
 * 아래는 통과형(pass-through) fetch만 하는 최소 구현입니다.
 * - 캐시를 사용하지 않으므로 대시보드가 항상 최신 데이터를 보여줍니다
 *   (일일 갱신 스냅샷 특성상 캐시 오염 위험이 없음).
 */
// strict 타입 검사: lib.webworker의 self는 WorkerGlobalScope로 선언되어 있어
// 서비스 워커 전용 API(skipWaiting/clients/FetchEvent)는 ServiceWorkerGlobalScope로 캐스트한다.
const swScope = /** @type {ServiceWorkerGlobalScope} */ (/** @type {unknown} */ (self));

swScope.addEventListener("install", () => {
  swScope.skipWaiting();
});

swScope.addEventListener("activate", (event) => {
  event.waitUntil(swScope.clients.claim());
});

swScope.addEventListener("fetch", (event) => {
  event.respondWith(fetch(event.request));
})