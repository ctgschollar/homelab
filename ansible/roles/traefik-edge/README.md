# Traefik Edge Role

This role deploys Traefik as an edge proxy on the dedicated edge node (192.168.3.91), acting as the external entry point for the homelab infrastructure.

## Architecture

The edge Traefik instance:
- Receives external HTTP/HTTPS traffic
- Routes traffic to internal Docker Swarm Traefik instances for load balancing
- Acts as the first tier in a two-tier Traefik setup

## Configuration

### Default Variables

- `traefik_edge_config_dir`: `/srv/traefik` - Configuration directory
- `traefik_edge_image`: `traefik:v3.1` - Docker image version
- `traefik_edge_container_name`: `traefik-edge` - Container name
- `traefik_edge_web_port`: `80` - HTTP port
- `traefik_edge_websecure_port`: `443` - HTTPS port
- `traefik_edge_log_level`: `INFO` - Log level

### Internal Load Balancer Targets

The role configures load balancing to internal Traefik instances:
- `192.168.3.71:443` (dks02)
- `192.168.3.72:443` (dks03)
- `192.168.3.73:443` (dks04) - currently commented out

### Service Routes

Currently configured routes:
- `jellyfin.schollar.dev`
- `jellyseerr.schollar.dev`
- `excalidraw.schollar.dev`
- `excalidraw-room.schollar.dev`
- `excalidraw-storage.schollar.dev`

## Usage

```yaml
- hosts: edge_nodes
  roles:
    - traefik-edge
```

## Files Generated

- `/srv/traefik/docker-compose.yaml` - Docker Compose configuration
- `/srv/traefik/traefik.yml` - Main Traefik configuration
- `/srv/traefik/dynamic/http.yml` - Dynamic HTTP routing rules

## Security Features

- Container runs with `no-new-privileges:true`
- `NET_RAW` capability dropped
- API dashboard disabled
- Configuration files mounted read-only

## Notes

- Internal TLS verification is currently disabled (`insecureSkipVerify: true`)
- All HTTP traffic is forced to HTTPS via middleware
- Configuration supports dynamic reloading via file watchers