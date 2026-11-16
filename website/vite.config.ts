import { defineConfig } from 'vite';
import react from '@vitejs/plugin-react';

// `base: './'` keeps every asset path relative, so the build works from any
// sub-path (S3 prefix, CloudFront behaviour, preview sandbox) without changes.
export default defineConfig({
  base: './',
  plugins: [react()],
  build: { outDir: 'dist', sourcemap: false, target: 'es2020' },
});
