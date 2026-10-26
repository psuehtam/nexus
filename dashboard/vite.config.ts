import { defineConfig } from 'vite';
import react from '@vitejs/plugin-react';

export default defineConfig({
  plugins: [react()],
  server: {
    host: '127.0.0.1',
    port: 3000,
    proxy: { '/api': { target: process.env.I9_API_URL || 'http://127.0.0.1:8000', changeOrigin: false } },
  },
  build: { outDir: 'dist', emptyOutDir: true },
});
