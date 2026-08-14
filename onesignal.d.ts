// OneSignal 웹 푸시 SDK(OneSignalSDK.page.js) 전역 타입 — 대시보드 인라인 JS strict 검사용 최소 스텁.
// CDN 스크립트가 런타임에 window.OneSignalDeferred / OneSignal 전역을 제공한다.
declare var OneSignal: any;
declare var OneSignalDeferred: Array<(OneSignal: any) => void>;
interface Window {
  OneSignalDeferred: Array<(OneSignal: any) => void>;
}
