# Cloudflared Tunnel Ansible Role

This role deploys and configures Cloudflare Tunnel (cloudflared) on a target host.

## Requirements

- Debian/Ubuntu-based system
- Internet connectivity to download cloudflared package
- Valid Cloudflare tunnel credentials

## Role Variables

### Required Variables

These must be defined in your playbook or inventory:

```yaml
# Tunnel credentials (structure from Cloudflare dashboard)
cloudflared_tunnel_credentials:
  AccountTag: "your-account-tag"
  TunnelSecret: "your-tunnel-secret-base64"
  TunnelID: "your-tunnel-id"
  Endpoint: "your-endpoint"

# Tunnel identification
cloudflared_tunnel_name: "your-tunnel-name"
cloudflared_tunnel_id: "your-tunnel-id"
```

### Optional Variables

```yaml
# Logging and performance
cloudflared_loglevel: "info"  # debug, info, warn, error, fatal
cloudflared_keepalive_timeout: "30s"
cloudflared_restart_sec: "3"

# Ingress rules (override the defaults as needed)
cloudflared_ingress_rules:
  - hostname: "example.com"
    service: "http://127.0.0.1:80"
    description: "Example service"
    origin_request:
      http2Origin: true
      noTLSVerify: false
```

## Example Playbook

```yaml
---
- hosts: edge_nodes
  become: yes
  roles:
    - cloudflared
  vars:
    cloudflared_tunnel_name: "my-tunnel"
    cloudflared_tunnel_id: "12345678-1234-1234-1234-123456789abc"
    cloudflared_tunnel_credentials:
      AccountTag: "abcdef1234567890"
      TunnelSecret: "base64-encoded-secret=="
      TunnelID: "12345678-1234-1234-1234-123456789abc"
      Endpoint: "xyz.api.cloudflare.com"
    cloudflared_ingress_rules:
      - hostname: "app.example.com"
        service: "http://127.0.0.1:8080"
        description: "My application"
        origin_request:
          http2Origin: true
```

## Security Note

The tunnel credentials contain sensitive information. Consider using Ansible Vault to encrypt these values:

```bash
ansible-vault encrypt_string 'your-secret-value' --name 'cloudflared_tunnel_credentials'
```

## Files Managed

- `/etc/cloudflared/config.yml` - Main configuration file
- `/etc/cloudflared/{tunnel-id}.json` - Tunnel credentials
- `/etc/systemd/system/cloudflared-tunnel.service` - Systemd service
- `/etc/apt/sources.list.d/cloudflared.list` - APT repository

## Services

- `cloudflared-tunnel.service` - Main tunnel service (enabled and started)