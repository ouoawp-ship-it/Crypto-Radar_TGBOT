/** @type {import('next').NextConfig} */
const nextConfig = {
  poweredByHeader: false,
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
