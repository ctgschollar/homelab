# Homepage with Auto-Discovery

Homepage is configured to automatically discover services from Docker Swarm using Docker labels.

## How It Works

Homepage reads service labels from the Docker socket and automatically displays them in the dashboard. Services are organized into groups and can have custom icons, descriptions, and ordering.

## Adding Services to Homepage

To make a service appear in Homepage, add these labels to the service's `deploy.labels` section:

```yaml
deploy:
  labels:
    # Required labels
    homepage.group: "Group Name"           # e.g., "Media", "Productivity", "HomeLab"
    homepage.name: "Service Name"          # Display name
    homepage.icon: "icon-name"             # Icon name or URL
    homepage.href: "https://url"           # Service URL

    # Optional labels
    homepage.description: "Description"    # Short description
    homepage.weight: "10"                  # Lower = higher priority in group
```

### Example: Adding Jellyfin to Homepage

```yaml
services:
  jellyfin:
    image: jellyfin/jellyfin:latest
    networks: [traefik-net]
    deploy:
      labels:
        traefik.enable: "true"
        # ... other Traefik labels ...

        # Homepage labels
        homepage.group: "Media"
        homepage.name: "Jellyfin"
        homepage.icon: "jellyfin.png"
        homepage.href: "https://jellyfin.schollar.dev"
        homepage.description: "Media server"
        homepage.weight: "10"
```

## Available Icons

Homepage supports:
- Built-in icons: `service-name.png` (e.g., `jellyfin.png`, `sonarr.png`)
- Simple Icons: `si-servicename` (e.g., `si-jellyfin`, `si-sonarr`)
- Custom URLs: Full URL to an image
- Mdi Icons: `mdi-icon-name` (Material Design Icons)

Browse available icons at:
- https://github.com/walkxcode/dashboard-icons
- https://simpleicons.org/

## Service Groups

Organize services into logical groups using the `homepage.group` label. Common groups:
- **Media**: Jellyfin, Sonarr, Radarr, etc.
- **Productivity**: Joplin, Obsidian, Hedgedoc, etc.
- **HomeLab**: Traefik, Portainer, Proxmox, etc.
- **Monitoring**: Prometheus, Grafana, etc.
- **Downloads**: qBittorrent, Prowlarr, etc.

Groups are automatically created when you add services with new group names.

## Deployment

After updating service labels:

1. Redeploy the homepage stack:
   ```bash
   docker stack deploy -c docker-compose.yaml homepage
   ```

2. Redeploy any services with new/updated labels:
   ```bash
   docker stack deploy -c docker-compose.yaml service-name
   ```

Homepage will automatically discover the new services within a few seconds.

## Layout Configuration

Edit `settings.yaml` to customize group layout:

```yaml
layout:
  Media:
    style: row
    columns: 3
  HomeLab:
    style: row
    columns: 4
```

## Keeping Manual Services

The existing `services.yaml` file is still active, so manually configured services (like Proxmox nodes) will continue to appear alongside auto-discovered services.

## Troubleshooting

If services don't appear:
1. Verify homepage is running on a manager node
2. Check Docker socket is mounted: `docker service inspect homepage_homepage`
3. Verify labels are on the service: `docker service inspect service-name`
4. Check homepage logs: `docker service logs homepage_homepage`

## Current Configuration

- **Docker Socket**: Mounted at `/var/run/docker.sock`
- **Swarm Mode**: Enabled
- **Static Services**: `services.yaml` (for non-Dockerized services)
- **Auto-Discovery**: `docker.yaml` + service labels
