<script setup lang="ts">
import { AlertCircle, CheckCircle2, WifiOff } from 'lucide-vue-next';

withDefaults(defineProps<{
  message?: string;
  tone?: 'error' | 'success' | 'info' | 'warning';
}>(), { message: '', tone: 'error' });
</script>

<template>
  <v-alert v-if="message" class="apex-ui-feedback" :type="tone" variant="tonal" role="alert">
    <template #prepend>
      <WifiOff v-if="tone === 'info'" :size="18" />
      <CheckCircle2 v-else-if="tone === 'success'" :size="18" />
      <AlertCircle v-else :size="18" />
    </template>
    {{ message }}
    <slot />
  </v-alert>
</template>
