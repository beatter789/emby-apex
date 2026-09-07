import type { Component } from 'vue';

export type ApexApp = 'admin' | 'portal';

export type ApexNavItem = {
  path: string;
  label: string;
  icon: Component;
};

export type AsyncState = 'idle' | 'loading' | 'success' | 'error' | 'offline';

export type SafeBackTarget = '/dashboard' | '/account';
