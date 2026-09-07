<template>
  <v-app class="apex-vuetify-app">
    <Transition name="apex-fade" mode="out-in">
      <slot />
    </Transition>
  </v-app>
</template>
