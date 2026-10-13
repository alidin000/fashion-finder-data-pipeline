import type { NextConfig } from "next";

// The browser calls the API on this origin (via the rewrite below) or on NEXT_PUBLIC_API_BASE_URL.
function apiOrigin() {
  try {
    const origin = new URL(process.env.NEXT_PUBLIC_API_BASE_URL ?? "").origin;
    return origin === "null" ? "" : ` ${origin}`;
  } catch {
    return "";
  }
}

// Conservative CSP without nonces, following the Next 16 guide: Next inlines its bootstrap scripts, and
// dev needs 'unsafe-eval'. Retailer image CDNs vary and photo previews are blob: URLs.
const contentSecurityPolicy = [
  "default-src 'self'",
  `script-src 'self' 'unsafe-inline'${process.env.NODE_ENV === "development" ? " 'unsafe-eval'" : ""}`,
  "style-src 'self' 'unsafe-inline'",
  "img-src 'self' https: data: blob:",
  `connect-src 'self'${apiOrigin()}`,
  "font-src 'self'",
  "object-src 'none'",
  "base-uri 'self'",
  "form-action 'self'",
  "frame-ancestors 'none'",
].join("; ");

const nextConfig: NextConfig = {
  reactStrictMode: true,
  // Self-contained server.js with only the traced node_modules, for the Docker image.
  output: "standalone",
  async rewrites() {
    const apiTarget = process.env.FASHION_FINDER_API_INTERNAL_URL ?? "http://127.0.0.1:8000";
    return [{ source: "/api/v1/:path*", destination: `${apiTarget}/api/v1/:path*` }];
  },
  async headers() {
    return [{ source: "/(.*)", headers: [{ key: "Content-Security-Policy", value: contentSecurityPolicy }] }];
  },
};

export default nextConfig;
