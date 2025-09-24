# Homelab Infrastructure

This repository contains Docker Swarm configurations for a homelab infrastructure running various self-hosted services.

## Architecture

- **Orchestration**: Docker Swarm mode
- **Reverse Proxy**: Traefik with Let's Encrypt SSL certificates via Cloudflare DNS-01 challenge
- **Domain**: `*.schollar.dev`
- **Storage**: CephFS for configs (`/mnt/cephfs-configs/`) and shared media (`/mnt/shared/`)
- **Monitoring**: Prometheus + Grafana stack with blackbox probes

## Services Overview

### Core Infrastructure
- **traefik**: Load balancer/reverse proxy with SSL termination
- **monitoring**: Prometheus + Grafana for metrics and dashboards
- **portainer**: Docker management UI
- **postgres**: Shared PostgreSQL database

### Media & Entertainment
- **jellyfin**: Media server
- **immich**: Photo management and AI-powered organization
- **radarr**: Movie collection manager
- **sonarr**: TV series collection manager
- **prowlarr**: Indexer manager
- **qbittorrent**: BitTorrent client
- **jellyseerr**: Media request management

### Productivity & Development
- **hedgedoc**: Collaborative markdown editor
- **codimd**: Alternative markdown editor
- **homepage**: Dashboard/landing page
- **clipcascade**: Clipboard management
- **red_ui**: Node-RED flow editor
- **registry**: Private Docker registry

### Network & Utilities
- **coredns**: DNS server
- **flaresolverr**: Cloudflare challenge solver
- **metrics**: Additional monitoring exporters

## Node Labels

Services are constrained to specific nodes using Docker Swarm labels:

- `node.labels.traefik == true`: Traefik instances
- `node.labels.media == true`: Media services (Immich, etc.)
- `node.labels.metrics == true`: Monitoring stack
- `node.role == manager`: Management services

## Configuration Management

- Environment files stored in `/mnt/cephfs-configs/{service}/.env`
- Secrets managed via Docker Swarm secrets
- Persistent data on bind mounts or named volumes
- All services use `traefik-net` external network for routing

## Deployment

Services are deployed using `docker stack deploy` with Docker Compose v3.8/3.9 files. Each service directory contains:

- `docker-compose.yaml`: Service definition
- Configuration files and secrets as needed

## SSL/TLS

All public services use Let's Encrypt certificates automatically provisioned by Traefik using Cloudflare DNS-01 challenge with `ctgschollar@gmail.com`.

## Monitoring

Prometheus blackbox monitoring is configured for all public endpoints with `prometheus.blackbox=true` and `metrics.probe_url` labels.

## Template Docker Compose

Use this template when adding new services to the homelab:

```yaml
networks:
  traefik-net:
    external: true

volumes:
  service_data:
    driver: linbit/linstor-docker-volume
    driver_opts:
      size: "10G"              # Adjust size as needed
      fs: "xfs"                # or ext4
      replicas: "2"            # Number of replicas for HA
      storagepool: "pool_ssd"  # or pool_hdd for slower storage

services:
  service-name:
    image: your/service:latest
    networks:
      - traefik-net
    volumes:
      - service_data:/app/data
      # Optional bind mounts for shared storage
      - type: bind
        source: /mnt/shared/service/cache
        target: /app/cache
    environment:
      # Service-specific environment variables
      SERVICE_URL: https://service.schollar.dev
    # Optional: env_file for secrets
    # env_file: /mnt/cephfs-configs/service/.env
    deploy:
      mode: replicated
      replicas: 1
      restart_policy:
        condition: on-failure
        delay: 5s
        max_attempts: 0
      placement:
        constraints:
          - node.labels.service == true  # Adjust constraint as needed
      update_config:
        order: start-first
        parallelism: 1
        failure_action: rollback
      labels:
        # Traefik configuration
        traefik.enable: "true"
        traefik.docker.network: traefik-net

        # HTTPS router
        traefik.http.routers.service.rule: Host(`service.schollar.dev`)
        traefik.http.routers.service.entrypoints: websecure
        traefik.http.routers.service.tls.certresolver: cf

        # Service port (adjust to your service's port)
        traefik.http.services.service.loadbalancer.server.port: "8080"

        # Prometheus monitoring
        prometheus.blackbox: "true"
        metrics.probe_url: "https://service.schollar.dev"

        # Optional: Direct Prometheus scraping if service exposes /metrics
        # prometheus.scrape: "true"
        # prometheus.path: "/metrics"
        # prometheus.port: "9090"
```

### Key Configuration Points

1. **Network**: Always use `traefik-net` external network
2. **Storage**: Use Linstor volumes for persistent data with HA
3. **Placement**: Set appropriate node label constraints
4. **Traefik Labels**: Configure hostname, SSL, and load balancer port
5. **Monitoring**: Enable blackbox probing for uptime monitoring
6. **Restart Policy**: Use `on-failure` for Swarm compatibility

## Recent Changes

- Restart policies updated to use `condition: on-failure` instead of `restart: always` for Swarm compatibility
- Services moved to linstor storage backend (sonarr, qbittorrent, prowlarr)
- Environment variable syntax fixes