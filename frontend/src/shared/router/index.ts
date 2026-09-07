import type { ApexApp, ApexNavItem, SafeBackTarget } from '../types';
import { createMemoryHistory, createRouter } from 'vue-router';

// The app keeps server-rendered URL entry points instead of a client SPA.
// This router is deliberately memory-backed and only provides shared route
// metadata/transition hooks for future page-level enhancement.
export const apexRouter = createRouter({
  history: createMemoryHistory(),
  routes: [],
});

export const adminFallback: SafeBackTarget = '/dashboard';
export const portalFallback: SafeBackTarget = '/account';

export function isAuthPath(pathname: string): boolean {
  return pathname === '/login' || pathname === '/register';
}

export function fallbackFor(app: ApexApp): SafeBackTarget {
  return app === 'admin' ? adminFallback : portalFallback;
}

export function canUseHistoryBack(referrer: string, pathname = window.location.pathname): boolean {
  if (window.history.length <= 1) return false;
  if (!referrer) return pathname !== '/login' && pathname !== '/register';
  try {
    return !isAuthPath(new URL(referrer, window.location.origin).pathname);
  } catch {
    return false;
  }
}

export function goBack(app: ApexApp): void {
  if (canUseHistoryBack(document.referrer)) window.history.back();
  else window.location.assign(fallbackFor(app));
}

export function navItem(path: string, label: string, icon: ApexNavItem['icon']): ApexNavItem {
  return { path, label, icon };
}
