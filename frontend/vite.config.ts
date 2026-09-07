import { defineConfig } from 'vite';
import vue from '@vitejs/plugin-vue';
import { resolve } from 'node:path';

export default defineConfig({
  // FastAPI serves the generated files below /static/frontend.
  base: '/static/frontend/',
  plugins: [vue()],
  build: {
    outDir: '../app/static/frontend',
    emptyOutDir: true,
    manifest: false,
    rollupOptions: {
      input: {
        admin: resolve(__dirname, 'admin.html'),
        portal: resolve(__dirname, 'portal.html'),
      },
      output: {
        entryFileNames: '[name].js',
        chunkFileNames: 'chunks/[name]-[hash].js',
        assetFileNames: 'assets/[name][extname]',
      },
    },
  },
});
