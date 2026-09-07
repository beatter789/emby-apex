<script setup lang="ts">
withDefaults(defineProps<{ label?: string; compact?: boolean }>(), { label: '正在加载…', compact: false });
</script>

<template>
  <div class="apex-ui-loading" :class="{ compact }" role="status" aria-live="polite">
    <v-progress-circular indeterminate color="primary" :size="compact ? 22 : 28" width="3" />
    <span>{{ label }}</span>
  </div>
</template>
