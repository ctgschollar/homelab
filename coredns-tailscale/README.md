# CoreDNS for Tailscale

This is a dedicated CoreDNS server for Tailscale DNS resolution, separate from the main CoreDNS instance.

## Purpose

Redirects all `*.schollar.dev` domain queries to `100.83.70.76` (Traefik server accessible via Tailscale).

This allows accessing Traefik-proxied services over Tailscale using proper domain names like `service.schollar.dev` instead of IP addresses.

## Configuration

- **DNS Port**: 5353 (UDP/TCP) on host
- **Target IP**: 100.83.70.76
- **Domains**: All `*.schollar.dev` and `schollar.dev`
- **Fallback DNS**: 1.1.1.1, 9.9.9.9 (Cloudflare) for other queries

## Deployment

Deploy via Docker Swarm:

```bash
docker stack deploy -c docker-compose.yaml coredns-tailscale
```

Verify deployment:

```bash
docker stack ps coredns-tailscale
docker service logs coredns-tailscale_coredns-tailscale
```

Remove deployment:

```bash
docker stack rm coredns-tailscale
```

## Testing

Test DNS resolution locally:

```bash
# Test wildcard resolution
dig @localhost -p 5353 test.schollar.dev

# Test apex domain
dig @localhost -p 5353 schollar.dev

# Test other domains (should forward to Cloudflare)
dig @localhost -p 5353 google.com
```

Expected response for `*.schollar.dev`:
```
;; ANSWER SECTION:
test.schollar.dev.      60      IN      A       100.83.70.76
```

## Tailscale Integration

### Option 1: Global Nameservers (Tailscale Admin Console)

1. Go to [Tailscale Admin Console](https://login.tailscale.com/admin/dns)
2. Under "Nameservers", add your server's Tailscale IP with port:
   - Format: `100.x.x.x:5353` (replace with your server's Tailscale IP)
3. This will make all Tailscale clients use this DNS server

### Option 2: Split DNS (Tailscale Admin Console)

1. Go to [Tailscale Admin Console](https://login.tailscale.com/admin/dns)
2. Under "Split DNS", add:
   - **Domain**: `schollar.dev`
   - **Nameserver**: `100.x.x.x:5353` (your server's Tailscale IP)
3. This only routes `*.schollar.dev` queries to this DNS server

### Option 3: Per-Client Configuration

On individual Tailscale clients:

```bash
# Set global nameserver
tailscale set --accept-dns=true

# Or configure split DNS
tailscale set --accept-dns=true --nameserver=100.x.x.x:5353
```

## Finding Your Server's Tailscale IP

```bash
# On the server running this CoreDNS container
tailscale ip -4
```

Use the returned IP (e.g., `100.83.70.76`) when configuring Tailscale DNS.

## Architecture

```
Tailscale Client
      ↓
   DNS Query: app.schollar.dev
      ↓
CoreDNS (port 5353)
      ↓
   Returns: 100.83.70.76
      ↓
Traefik (100.83.70.76)
      ↓
   Routes to backend service
```

## Troubleshooting

### DNS Not Resolving

1. Check if service is running:
   ```bash
   docker service ps coredns-tailscale_coredns-tailscale
   ```

2. Check logs for errors:
   ```bash
   docker service logs -f coredns-tailscale_coredns-tailscale
   ```

3. Verify port is listening:
   ```bash
   netstat -ulnp | grep 5353
   ```

### Tailscale Not Using Custom DNS

1. Ensure `--accept-dns=true` is set on clients
2. Verify Tailscale admin console shows the correct nameserver
3. Check Tailscale status: `tailscale status`
4. Force DNS refresh: `tailscale netcheck`

## Notes

- Port 5353 is used because port 53 is already taken by the main CoreDNS instance
- The server runs in `replicated` mode (1 replica) on manager nodes
- Uses the existing `dns-net` overlay network
- All non-schollar.dev queries are forwarded to Cloudflare DNS (1.1.1.1)
