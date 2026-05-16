/** @type {import('next').NextConfig} */
const nextConfig = {
  reactStrictMode: true,
  images: {
    remotePatterns: [
      { protocol: "https", hostname: "**.javbus.com" },
      { protocol: "https", hostname: "**.dmm.co.jp" },
    ],
  },
};

export default nextConfig;
