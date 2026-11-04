import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'

// Tauri expects a fixed port and must not watch Rust/Python build output: on Windows the
// watcher otherwise hits EBUSY on locked files under src-tauri/target and binaries/.
export default defineConfig({
  plugins: [react()],
  clearScreen: false,
  server: {
    host: '127.0.0.1',
    port: 1420,
    strictPort: true,
    watch: {
      ignored: [
        '**/src-tauri/target/**',
        '**/src-tauri/binaries/**',
        '**/src-tauri/gen/**',
        '**/dist/**',
        '**/node_modules/**',
        '**/.venv/**',
      ],
    },
  },
  envPrefix: ['VITE_', 'TAURI_ENV_'],
  build: {
    target: 'chrome110',
    outDir: 'dist',
    emptyOutDir: true,
    sourcemap: false,
  },
})
