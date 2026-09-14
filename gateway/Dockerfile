# v1-gateway: the /v1 relay routine deploys never restart (2026-09-13).
#
# No npm install, on purpose: node: built-ins only, so this image changes only
# when its inputs change, and the launcher pins its tag to a digest of exactly
# those inputs (Dockerfile, server.cjs, every file under lib/), so a test or
# README edit never recreates the container. The base is the same pinned
# node:20-alpine index digest as frontend/Dockerfile, so both edges run the
# runtime the tests ran on.
FROM node:20-alpine@sha256:fb4cd12c85ee03686f6af5362a0b0d56d50c58a04632e6c0fb8363f609372293

ENV NODE_ENV=production \
    V1_GATEWAY_PORT=8090 \
    V1_GATEWAY_SPOOL_DIR=/spool

WORKDIR /app
COPY server.cjs ./server.cjs
COPY lib ./lib

# The spool holds request bodies above 1 MiB while they may still have to be
# re-sent; 0700 and owned by the unprivileged user that writes it. Compose
# mounts a named volume here (the root filesystem is read-only).
RUN mkdir -p /spool && chown node:node /spool && chmod 0700 /spool

USER node
EXPOSE 8090

# EXEC FORM: with compose's `init: true`, tini is PID 1 and forwards SIGTERM
# straight to node, whose handler (lib/drain.cjs) closes the listener and
# cuts in-flight relays at +2 s. A shell form would put /bin/sh in between
# and the drain would never run.
CMD ["node", "server.cjs"]
