<script setup lang="ts">
withDefaults(defineProps<{
  modelValue?: string | number;
  label?: string;
  type?: string;
  autocomplete?: string;
  placeholder?: string;
  required?: boolean;
  disabled?: boolean;
  autofocus?: boolean;
}>(), {
  modelValue: '',
  label: '',
  type: 'text',
  autocomplete: 'off',
  placeholder: '',
  required: false,
  disabled: false,
  autofocus: false,
});
defineEmits<{ 'update:modelValue': [value: string] }>();
</script>

<template>
  <v-text-field
    class="apex-ui-text-field"
    :model-value="modelValue"
    :label="label"
    :type="type"
    :autocomplete="autocomplete"
    :placeholder="placeholder"
    :required="required"
    :disabled="disabled"
    :autofocus="autofocus"
    v-bind="$attrs"
    @update:model-value="$emit('update:modelValue', $event)"
  />
</template>
