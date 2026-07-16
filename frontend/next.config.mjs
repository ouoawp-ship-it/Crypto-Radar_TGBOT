/** @type {import('next').NextConfig} */
const nextConfig = {
  poweredByHeader: false,
  async headers() {
    return [
      {
        source: "/:path*",
        headers: [
          { key: "X-Content-Type-Options", value: "nosniff" },
          { key: "X-Frame-Options", value: "DENY" },
          { key: "Referrer-Policy", value: "strict-origin-when-cross-origin" },
          { key: "Permissions-Policy", value: "camera=(), microphone=(), geolocation=()" },
          { key: "Strict-Transport-Security", value: "max-age=31536000" }
        ]
      }
    ];
  },
  async rewrites() {
    const devApiProxy = process.env.PAOXX_DEV_API_PROXY;
    if (process.env.NODE_ENV !== "development" || !devApiProxy) return [];
    return [
      {
        source: "/public-api/:path*",
        destination: `${devApiProxy.replace(/\/$/, "")}/public-api/:path*`
      }
    ];
  }
};

export default nextConfig;
