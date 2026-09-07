<script setup lang="ts">
import type { ApexNavItem } from '../types';

const props = withDefaults(defineProps<{
  items: readonly ApexNavItem[];
  activePath: string;
  ariaLabel?: string;
}>(), { ariaLabel: '移动端导航' });
</script>

<template>
  <nav class="apex-mobile-dock" :aria-label="props.ariaLabel">
    <div class="apex-mobile-dock-card">
      <div class="apex-mobile-dock-toggle" role="list">
        <a
          v-for="item in props.items"
          :key="item.path"
          class="apex-mobile-dock-btn"
          :class="{ on: props.activePath === item.path }"
          :href="item.path"
          :aria-current="props.activePath === item.path ? 'page' : undefined"
          role="listitem"
        >
          <span class="apex-mobile-dock-btn-content">
            <component :is="item.icon" :size="24" :stroke-width="1.8" aria-hidden="true" />
            <span>{{ item.label }}</span>
          </span>
        </a>
      </div>
    </div>
  </nav>
</template>
