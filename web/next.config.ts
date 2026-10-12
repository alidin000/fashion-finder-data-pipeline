import type { NextConfig } from "next";

const nextConfig: NextConfig = {
  reactStrictMode: true,
  async rewrites() {
    const apiTarget = process.env.FASHION_FINDER_API_INTERNAL_URL ?? "http://127.0.0.1:8000";
    return [{ source: "/api/v1/:path*", destination: `${apiTarget}/api/v1/:path*` }];
  },
};

export default nextConfig;
