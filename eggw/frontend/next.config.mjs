/** @type {import('next').NextConfig} */
const nextConfig = {
  reactStrictMode: true,
  // EggW runs as a local UI. Prefer deterministic full-length chunk responses
  // over Next dev's gzip/chunked responses, which can surface as browser-only
  // ChunkLoadError / syntax errors when a compressed chunk is cut short.
  compress: false,
};

export default nextConfig;
