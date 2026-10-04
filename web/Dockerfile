# Ledger web GUI: Vite build, served by nginx. nginx proxies /api/ to the api
# service (prefix stripped), with SSE (no buffering) and WebSocket upgrade for
# the agent shell. Build arg VITE_MOCK=1 bundles the mock adapter instead of
# the real API (demo/screenshots only).
FROM node:22-alpine AS build
WORKDIR /web
COPY package.json package-lock.json ./
RUN npm ci --no-audit --no-fund
COPY . .
ARG VITE_MOCK=""
RUN if [ "$VITE_MOCK" = "1" ]; then npx vite build --mode mock; else npm run build; fi

FROM nginx:alpine
COPY nginx.conf /etc/nginx/conf.d/default.conf
COPY --from=build /web/dist /usr/share/nginx/html
EXPOSE 80
