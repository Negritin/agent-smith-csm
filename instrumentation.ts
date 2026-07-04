import * as Sentry from "@sentry/nextjs";

export async function register() {
    if (process.env.NEXT_RUNTIME === "nodejs") {
        // Server-side Sentry initialization
        Sentry.init({
            dsn: process.env.SENTRY_DSN,
            tracesSampleRate: 1.0,
            debug: false,
        });
    }

    if (process.env.NEXT_RUNTIME === "edge") {
        // Edge runtime Sentry initialization
        Sentry.init({
            dsn: process.env.SENTRY_DSN,
            tracesSampleRate: 1.0,
            debug: false,
        });
    }
}

export const onRequestError = Sentry.captureRequestError;
