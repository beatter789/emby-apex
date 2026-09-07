<script setup lang="ts">
withDefaults(defineProps<{
  type?: 'button' | 'submit' | 'reset';
  loading?: boolean;
  disabled?: boolean;
  color?: string;
  variant?: 'flat' | 'tonal' | 'outlined' | 'text' | 'plain' | 'elevated';
  block?: boolean;
}>(), {
  type: 'button',
  loading: false,
  disabled: false,
  color: 'primary',
  variant: 'flat',
  block: false,
});
</script>

<template>
  <v-btn
    class="apex-ui-button"
    :type="type"
    :loading="loading"
    :disabled="disabled"
    :color="color"
    :variant="variant"
    :block="block"
    v-bind="$attrs"
  >
    <slot />
  </v-btn>
</template>
