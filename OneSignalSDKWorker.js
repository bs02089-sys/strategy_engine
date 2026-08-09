/* 스윙 투자 알리미 — OneSignal 웹 푸시 + PWA 통합 서비스 워커
 *
 * - OneSignal 웹 푸시: CDN의 OneSignal SDK 워커 로드 (실제 알림 로직 담당)
 * - PWA 설치: install/activate/fetch 핸들러를 이 파일에 통합
 *   → 같은 스코프(/strategy_engine/)에 워커가 1개만 존재하므로
 *     sw.js(구 PWA 워커)와의 충돌로 인한 '서비스 워커 등록 실패' 오류가 사라진다.
 */
importScripts("https://cdn.onesignal.com/sdks/web/v16/OneSignalSDK.sw.js");

// ── PWA 설치 지원 (통과형: 캐시 없음, 항상 최신 대시보드 표시) ──
self.addEventListener("install", () => {
  self.skipWaiting();
});

self.addEventListener("activate", (event) => {
  event.waitUntil(self.clients.claim());
});

self.addEventListener("fetch", (event) => {
  // navigation(페이지 이동)만 처리 — OneSignal SDK 워커의
  // 내부 요청(push 전달/추적 등)을 가로채지 않도록 다른 요청은 무시
  if (event.request.mode === "navigate") {
    // GitHub Pages 의 HTTP 캐시(max-age=600)를 우회해 항상 최신 대시보드를 가져온다.
    // 설치형 PWA가 오래된 화면을 보여주는 캐시 지연을 방지 (no-store: 캐시 읽기/쓰기 모두 금지)
    event.respondWith(fetch(event.request, { cache: "no-store" }));
  }
});
