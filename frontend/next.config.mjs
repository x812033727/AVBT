/** @type {import('next').NextConfig} */
const nextConfig = {
  reactStrictMode: true,
  // Self-contained server bundle: the Docker runner stage copies
  // .next/standalone + .next/static instead of the whole node_modules.
  output: "standalone",
  images: {
    remotePatterns: [
      { protocol: "https", hostname: "**.javbus.com" },
      { protocol: "https", hostname: "**.dmm.co.jp" },
    ],
  },
};

export default nextConfig;
