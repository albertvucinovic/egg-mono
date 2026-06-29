/** @type {import('next').NextConfig} */
const nextConfig = {
  reactStrictMode: true,
};

if (process.env.EGGW_NEXT_DIST_DIR) {
  nextConfig.distDir = process.env.EGGW_NEXT_DIST_DIR;
}

export default nextConfig;
