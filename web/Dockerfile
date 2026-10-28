FROM --platform=$BUILDPLATFORM node:22-bookworm-slim AS builder
WORKDIR /web
COPY web/package.json web/package-lock.json ./
RUN npm ci --ignore-scripts
COPY web/ ./
RUN npm run build

FROM nginx:stable-alpine
LABEL org.opencontainers.image.title="Airkeep web" \
      org.opencontainers.image.source="https://github.com/danohn/airkeep" \
      org.opencontainers.image.description="Airkeep dashboard and API reverse proxy"
COPY --from=builder /web/dist /usr/share/nginx/html
COPY web/nginx/ /etc/airkeep/
COPY LICENSE /usr/share/licenses/airkeep/LICENSE
USER 101:101
EXPOSE 8080 8443
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 CMD wget -q -O /dev/null http://127.0.0.1:8080/web-health || exit 1
ENTRYPOINT ["/bin/sh", "/etc/airkeep/entrypoint.sh"]
