import { defineConfig } from 'vitest/config'
import vue from '@vitejs/plugin-vue'
import { execFileSync } from 'node:child_process'
import { readFileSync } from 'node:fs'
import { fileURLToPath } from 'node:url'

const host = process.env.TAURI_DEV_HOST;
const projectRoot = fileURLToPath(new URL('.', import.meta.url))
const tauriConfig = JSON.parse(readFileSync(
  new URL('./src-tauri/tauri.conf.json', import.meta.url),
  'utf8',
)) as { version?: string }
let buildCommit = process.env.VITE_APP_BUILD_COMMIT?.trim() || ''
if (!buildCommit) {
  try {
    buildCommit = execFileSync('git', ['rev-parse', '--short=12', 'HEAD'], {
      cwd: projectRoot,
      encoding: 'utf8',
    }).trim()
  } catch {
    buildCommit = 'development'
  }
}

// https://vite.dev/config/
export default defineConfig({
  cacheDir: process.env.MKLINK_VITE_CACHE_DIR || 'node_modules/.vite',
  plugins: [vue()],
  build: {
    // Retire cached incorrect MIME headers, including unchanged lazy chunks/CSS.
    // Let Vite rewrite the entire graph; HTML-only URL rewriting misses preloads.
    // Bump this epoch only when asset response semantics change, not each build.
    assetsDir: 'assets/mime-v1',
  },
  define: {
    __APP_VERSION__: JSON.stringify(tauriConfig.version || '0.0.0'),
    __APP_BUILD_COMMIT__: JSON.stringify(buildCommit),
  },
  test: {
    environment: 'happy-dom',
    globals: true,
    // Memory gates use process.memoryUsage(); parallel files contaminate their baseline.
    fileParallelism: false,
    // The Windows release gate runs several large mount-heavy files serially.
    // Keep a bounded budget that tolerates host contention without weakening
    // explicit waitFor/assertion timeouts inside individual tests.
    testTimeout: 15_000,
    hookTimeout: 15_000,
  },
  clearScreen: false,
  server: {
    port: 5173,
    strictPort: true,
    host: host || false,
    hmr: host
      ? {
          protocol: "ws",
          host,
          port: 5174,
        }
      : undefined,
    watch: {
      ignored: ["**/src-tauri/**"],
    },
    proxy: {
      '/api': 'http://127.0.0.1:8765',
      '/ws': {
        target: 'ws://127.0.0.1:8765',
        ws: true,
      },
    },
  },
})
