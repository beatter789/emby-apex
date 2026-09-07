import { createApp, h, type Component } from 'vue';
import { AppShell } from './layout';
import apexVuetify from './vuetify';

export function mountApex(root: Element, component: Component, props?: Record<string, unknown>): void {
  createApp({
    render: () => h(AppShell, null, { default: () => h(component, props) }),
  }).use(apexVuetify).mount(root);
}
