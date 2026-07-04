/** @type {import('next').NextConfig} */
const { withSentryConfig } = require("@sentry/nextjs");

const nextConfig = {
  eslint: {
    ignoreDuringBuilds: true,
  },
  images: { unoptimized: true },
  webpack: (config) => {
    config.ignoreWarnings = [
      { module: /node_modules\/@supabase\/realtime-js/ },
    ];
    return config;
  },
};

// withSentryConfig é SEMPRE aplicado: preserva a instrumentação/runtime do Sentry
// (captura de erros continua via DSN, intacta). org/projeto só entram se vierem por
// env (SENTRY_ORG/SENTRY_PROJECT) — assim não cravamos o org do dono no código. Sem
// eles o upload de source map é só pulado (ele já exige SENTRY_AUTH_TOKEN de todo jeito).
const sentryBuildOpts = { silent: true };
if (process.env.SENTRY_ORG && process.env.SENTRY_PROJECT) {
  sentryBuildOpts.org = process.env.SENTRY_ORG;
  sentryBuildOpts.project = process.env.SENTRY_PROJECT;
}

module.exports = withSentryConfig(nextConfig, sentryBuildOpts);