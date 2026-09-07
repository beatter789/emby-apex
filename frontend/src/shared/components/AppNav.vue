<script setup lang="ts">
import type { ApexNavItem } from '../types';

const props = withDefaults(defineProps<{
  items: readonly ApexNavItem[];
  activePath?: string;
  ariaLabel?: string;
  selectable?: boolean;
}>(), { activePath: '', ariaLabel: '页面导航', selectable: false });
const emit = defineEmits<{ select: [item: ApexNavItem] }>();

function select(item: ApexNavItem, event: MouseEvent): void {
  if (!props.selectable) return;
  event.preventDefault();
  emit('select', item);
}
</script>

<template>
  <nav class="apex-nav-list" :aria-label="props.ariaLabel">
    <template v-for="item in props.items" :key="item.path">
      <button v-if="props.selectable" type="button" class="nav-link" :class="{ on: activePath === item.path }" @click="select(item, $event)">
        <component :is="item.icon" :size="17" /><span class="nav-label">{{ item.label }}</span>
      </button>
      <a v-else class="nav-link" :class="{ on: activePath === item.path }" :href="item.path">
        <component :is="item.icon" :size="17" /><span class="nav-label">{{ item.label }}</span>
      </a>
    </template>
  </nav>
</template>
