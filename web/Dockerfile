FROM node:24-bookworm-slim AS build

WORKDIR /app
COPY package.json package-lock.json ./
RUN npm ci
COPY . .
ENV FASHION_FINDER_API_INTERNAL_URL=http://api:8000
RUN npm run build

# output: "standalone" emits server.js plus only the traced node_modules, so no second npm ci.
# The rewrite target is fixed into the build's routes manifest; the runtime ENV is kept for parity.
FROM node:24-bookworm-slim
ENV NODE_ENV=production
ENV FASHION_FINDER_API_INTERNAL_URL=http://api:8000
# Docker sets HOSTNAME to the container ID; server.js binds to HOSTNAME, so listen on all interfaces.
ENV HOSTNAME=0.0.0.0
ENV PORT=3000
WORKDIR /app
COPY --from=build /app/.next/standalone ./
COPY --from=build /app/.next/static ./.next/static
RUN mkdir -p .next/cache && chown -R node:node .next/cache
USER node
EXPOSE 3000
CMD ["node", "server.js"]
