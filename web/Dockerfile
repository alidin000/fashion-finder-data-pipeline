FROM node:24-bookworm-slim AS build

WORKDIR /app
COPY package.json package-lock.json ./
RUN npm ci
COPY . .
ENV FASHION_FINDER_API_INTERNAL_URL=http://api:8000
RUN npm run build

FROM node:24-bookworm-slim
ENV NODE_ENV=production
ENV FASHION_FINDER_API_INTERNAL_URL=http://api:8000
WORKDIR /app
COPY --from=build /app/package.json /app/package-lock.json ./
RUN npm ci --omit=dev
COPY --from=build /app/.next ./.next
COPY --from=build /app/next.config.ts ./
RUN mkdir -p .next/cache && chown -R node:node .next/cache
USER node
EXPOSE 3000
CMD ["npm", "run", "start", "--", "-H", "0.0.0.0"]
