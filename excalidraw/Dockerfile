FROM node:20-alpine AS build

WORKDIR /app

# Clone Excalidraw repository
RUN apk add --no-cache git && \
    git clone https://github.com/excalidraw/excalidraw.git . && \
    git checkout master

# Install dependencies
RUN yarn install --frozen-lockfile

# Build-time environment variables for Vite
ARG VITE_APP_WS_SERVER_URL
ARG VITE_APP_HTTP_STORAGE_BACKEND_URL
ARG VITE_APP_LIBRARY_URL
ARG VITE_APP_LIBRARY_BACKEND

ENV VITE_APP_WS_SERVER_URL=${VITE_APP_WS_SERVER_URL}
ENV VITE_APP_HTTP_STORAGE_BACKEND_URL=${VITE_APP_HTTP_STORAGE_BACKEND_URL}
ENV VITE_APP_LIBRARY_URL=${VITE_APP_LIBRARY_URL}
ENV VITE_APP_LIBRARY_BACKEND=${VITE_APP_LIBRARY_BACKEND}

# Build the application
RUN yarn build:app:docker

# Production stage
FROM nginx:alpine

COPY --from=build /app/excalidraw-app/build /usr/share/nginx/html

# Configure nginx
RUN echo 'server { \
    listen 80; \
    location / { \
        root /usr/share/nginx/html; \
        index index.html; \
        try_files $uri $uri/ /index.html; \
    } \
}' > /etc/nginx/conf.d/default.conf

EXPOSE 80

CMD ["nginx", "-g", "daemon off;"]
