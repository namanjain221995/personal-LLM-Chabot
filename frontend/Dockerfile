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
# THE RUNTIME NEEDS `node`, AND NOTHING ELSE THAT SHIPS BESIDE IT.
#
# CMD below is `node ... server.js` against Next's standalone output: npm,
# npx, yarn and corepack are never invoked in this stage, and the packages
# they vendor are the only CRITICAL findings the image has. Measured
# 2026-09-23 with the pinned scanner and the pipeline's own blocking policy
# (`trivy image --scanners vuln --severity CRITICAL --ignore-unfixed
# --exit-code 1`):
#
#   node:20-alpine  usr/local/lib/node_modules/npm/.../tar  6.2.1  -> exit 1
#   node:22-alpine  the same path,                          7.5.11 -> exit 1
#
# both CVE-2026-59873, "node-tar: Denial of Service via crafted gzip bomb",
# fixed in 7.5.19. A DIGEST BUMP CANNOT FIX THIS: node:20-alpine's current
# index digest IS the one pinned above, and node:22-alpine — a whole major
# newer, Alpine 3.24, built 2026-09-17 — still bundles a tar below the fix.
# Every Node image is red until npm's own bundle moves.
#
# Deleting them is the fix rather than a workaround: the vulnerable code is
# genuinely unreachable here, and an image that does not carry a package
# cannot be vulnerable through it. The deps and build stages keep their npm;
# they are not what runs in production and are not what this gate scans.
RUN rm -rf \
      /usr/local/lib/node_modules/npm \
      /usr/local/lib/node_modules/corepack \
      /usr/local/bin/npm /usr/local/bin/npx /usr/local/bin/corepack \
      /opt/yarn-v* /usr/local/bin/yarn /usr/local/bin/yarnpkg \
 && node --version
COPY --from=build /app/public ./public
COPY --from=build --chown=nextjs:nodejs /app/.next/standalone ./
COPY --from=build --chown=nextjs:nodejs /app/.next/static ./.next/static
# The server preload (2026-09-13, no-timeout design): Node's 300 s request
# timeout off, a 60 s body-idle guard in its place, keep-alive above
# cloudflared's 90 s, and a SIGTERM drain that cuts /v1 relays at +2 s and chat
# streams at +15 s so one long relay no longer holds a deploy for 5 minutes.
# Not part of Next's standalone output, so it is copied on its own.
COPY --from=build --chown=nextjs:nodejs /app/server-preload.cjs ./server-preload.cjs
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
#
# `--require ./server-preload.cjs` (2026-09-13) keeps all three links: node is
# still PID 2 with no shell, and the preload's own SIGTERM listener only
# decides which connections to cut and when — Next's handler still does the
# server.close() and the exit, now as soon as the uploads still in flight end
# instead of when the longest relay does (tests/server-preload.test.ts).
CMD ["node", "--require", "./server-preload.cjs", "server.js"]
