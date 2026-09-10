# TechSara frontend — multi-stage build, arm64-compatible, Next.js standalone output.
# The node:20-alpine index digest is pinned so a rebuild months from now
# produces the same runtime as the one this repository was verified against.

# ---- deps ----
FROM node:20-alpine@sha256:fb4cd12c85ee03686f6af5362a0b0d56d50c58a04632e6c0fb8363f609372293 AS deps
WORKDIR /app
COPY package.json package-lock.json ./
RUN npm ci --no-audit --no-fund

# ---- build ----
FROM node:20-alpine@sha256:fb4cd12c85ee03686f6af5362a0b0d56d50c58a04632e6c0fb8363f609372293 AS build
WORKDIR /app
ENV NEXT_TELEMETRY_DISABLED=1
COPY --from=deps /app/node_modules ./node_modules
COPY . .
RUN npm run build

# ---- run ----
FROM node:20-alpine@sha256:fb4cd12c85ee03686f6af5362a0b0d56d50c58a04632e6c0fb8363f609372293 AS run
WORKDIR /app
ENV NODE_ENV=production \
    NEXT_TELEMETRY_DISABLED=1 \
    PORT=3000 \
    HOSTNAME=0.0.0.0
RUN addgroup -S nodejs && adduser -S nextjs -G nodejs
COPY --from=build /app/public ./public
COPY --from=build --chown=nextjs:nodejs /app/.next/standalone ./
COPY --from=build --chown=nextjs:nodejs /app/.next/static ./.next/static
USER nextjs
EXPOSE 3000
# EXEC FORM, AND IT HAS TO STAY THAT WAY.
#
# A graceful stop of this container is a three-link chain and every link was
# checked on 2026-09-11 rather than assumed (INF-1):
#
#   1. compose.yaml sets `init: true`, so docker-init (tini) is PID 1 and
#      forwards SIGTERM to its direct child.
#   2. This CMD is exec form, so that child is `node server.js` — no shell in
#      between to swallow the signal. Rewriting it as `CMD node server.js`
#      would put /bin/sh at PID 2, and sh does not forward signals to its
#      child: the proxy would then die only on SIGKILL, which is the bug this
#      chain exists to prevent.
#   3. Next 16's standalone server (node_modules/next/dist/server/lib/
#      start-server.js) installs SIGINT/SIGTERM handlers that call
#      server.close() and exit 143 once it resolves. In production it does
#      NOT call closeAllConnections(), so requests already in flight — a
#      64 MiB chunked part PUT, a proxied SSE frame — are allowed to finish.
#
# The time to use that is compose.yaml's `stop_grace_period: 5m`; without it
# Docker's 10 s default made the handler's politeness irrelevant.
CMD ["node", "server.js"]
