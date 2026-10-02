/** @type {import('next').NextConfig} */
const nextConfig = {
  output: "standalone",
  reactStrictMode: true,
  experimental: {
    // proxy.ts buffers request bodies and silently truncates anything past
    // this limit (default 10MB). Deployment uploads are up to 25MB
    // (backend/deploy/config.py::MAX_UPLOAD_BYTES) plus multipart overhead.
    proxyClientMaxBodySize: "26mb",
  },
  async rewrites() {
    return [
      {
        source: "/api/:path*",
        destination: process.env.BACKEND_API_INTERNAL_URL || "http://127.0.0.1:8000/api/:path*",
      },
    ];
  },
};

module.exports = nextConfig;

