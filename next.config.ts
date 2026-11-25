import type { NextConfig } from "next";

const config: NextConfig = {
  agentRules: false,
  async rewrites() {
    if (process.env.NODE_ENV !== "development") return [];

    const origin = (process.env.FLASK_ORIGIN ?? "http://127.0.0.1:5000").replace(/\/+$/, "");
    return ["/api/auth", "/api/chat", "/api/warm"].map((path) => ({
      source: path,
      destination: `${origin}${path}`,
    }));
  },
};

export default config;
