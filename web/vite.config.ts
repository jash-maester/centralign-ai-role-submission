/// <reference types="vitest" />
import { defineConfig, loadEnv } from 'vite';
import react from '@vitejs/plugin-react';

// `--mode mock` (or VITE_MOCK=1) builds against the in-browser mock adapter.
// The default build talks to the real API under /api (nginx proxies it).
export default defineConfig(({ mode }) => {
  const env = loadEnv(mode, process.cwd(), '');
  const mock = mode === 'mock' || env.VITE_MOCK === '1';
  return {
    plugins: [react()],
    define: {
      'import.meta.env.VITE_MOCK': JSON.stringify(mock ? '1' : ''),
    },
    server: {
      proxy: {
        '/api': {
          target: env.API_URL || 'http://localhost:8000',
          changeOrigin: true,
          ws: true,
          rewrite: (p: string) => p.replace(/^\/api/, ''),
        },
      },
    },
    build: {
      chunkSizeWarningLimit: 1500,
    },
    test: {
      environment: 'node',
      include: ['src/**/*.test.ts'],
    },
  };
});
