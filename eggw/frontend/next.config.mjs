/** @type {import('next').NextConfig} */
const nextConfig = {
  reactStrictMode: true,
  // EggW runs as a local UI. Prefer deterministic full-length chunk responses
  // over Next dev's gzip/chunked responses, which can surface as browser-only
  // ChunkLoadError / syntax errors when a compressed chunk is cut short.
  compress: false,
};

if (process.env.EGGW_NEXT_DIST_DIR) {
  nextConfig.distDir = process.env.EGGW_NEXT_DIST_DIR;
}

if (process.env.EGGW_NEXT_TSCONFIG_PATH) {
  nextConfig.typescript = {
    ...nextConfig.typescript,
    tsconfigPath: process.env.EGGW_NEXT_TSCONFIG_PATH,
  };
}

export default nextConfig;
