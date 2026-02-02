# Linstor Volume Backup Playbook

Automated backup system for Docker volumes using the linstor driver. This playbook deploys a systemd timer that runs daily backups of all linstor volumes to local storage.

## Features

- Automatically discovers all linstor Docker volumes
- Creates compressed tar.gz backups daily at 2 AM
- Maintains backups for 7 days (configurable)
- Stores backups in `/var/lib/docker-volume-backups/` (included in Proxmox Backup Server)
- Provides detailed logging to `/var/log/linstor-volume-backup.log`
- Creates a `latest.tar.gz` symlink for easy access to most recent backup
- Includes `volume-backups` CLI tool for easy management and restoration

## What Gets Deployed

This playbook installs the following on each node:

- `/usr/local/bin/backup-linstor-volumes.sh` - Backup script
- `/usr/local/bin/volume-backups` - Volume management CLI tool
- `/etc/systemd/system/linstor-volume-backup.service` - Systemd service
- `/etc/systemd/system/linstor-volume-backup.timer` - Systemd timer (daily at 2 AM)
- `/root/volume-backups-help.txt` - Quick reference guide
- `/var/lib/docker-volume-backups/` - Backup storage directory

## Prerequisites

- Ansible SSH keys deployed to all nodes (see `../README.md`)
- Docker and linstor-docker-volume driver installed on nodes
- Proxmox Backup Server configured to backup `/var/lib/` on nodes

## Deployment

### Deploy to all nodes:

```bash
cd /home/chris/Documents/git/homelab/ansible
ansible-playbook -i inventory.yml linstor-backup/playbook.yml
```

### Deploy to specific node:

```bash
ansible-playbook -i inventory.yml linstor-backup/playbook.yml --limit dks01
```

### Check deployment (dry-run):

```bash
ansible-playbook -i inventory.yml linstor-backup/playbook.yml --check
```

## Configuration

### Customize Variables

In `playbook.yml`, you can modify:

```yaml
vars:
  backup_dir: /var/lib/docker-volume-backups  # Backup storage location
  retention_days: 7                            # Days to keep old backups
  backup_time: "02:00:00"                      # Daily backup time
```

### Update Backup Script

In `files/backup-linstor-volumes.sh`, you can modify:
- `RETENTION_DAYS` - How many days to keep backups
- `BACKUP_DIR` - Where to store backups
- `LOG_FILE` - Where to write logs

## Testing

### Manually trigger a backup:

```bash
# On the Proxmox node
sudo systemctl start linstor-volume-backup.service
```

### Check backup status:

```bash
sudo systemctl status linstor-volume-backup.service
```

### View backup logs:

```bash
sudo tail -f /var/log/linstor-volume-backup.log
```

### Check timer schedule:

```bash
sudo systemctl list-timers linstor-volume-backup.timer
```

### List backups:

```bash
sudo ls -lh /var/lib/docker-volume-backups/
```

## Managing and Restoring Volumes

The playbook installs a `volume-backups` CLI tool on each node for easy backup management.

### List all backed up volumes:

```bash
volume-backups list
```

Output:
```
Backed Up Volumes                Backups    Latest Backup
======================================================================
sonarr_config                    7          2026-01-31 08:45:23
radarr_config                    7          2026-01-31 08:47:15
prowlarr_config                  5          2026-01-31 08:48:01
```

### List backups for a specific volume:

```bash
volume-backups sonarr_config list
```

Output:
```
Backups for volume: sonarr_config
================================================================================
#    Timestamp            Size         File
--------------------------------------------------------------------------------
1    2026-01-31 08:45:23  45.2 MB      sonarr_config_20260131-084523.tar.gz
2    2026-01-30 02:00:15  44.8 MB      sonarr_config_20260130-020015.tar.gz
3    2026-01-29 02:00:08  44.5 MB      sonarr_config_20260129-020008.tar.gz
```

### Restore a volume (interactive):

```bash
volume-backups restore sonarr_config
```

This will:
1. Show all available backups with timestamps and sizes
2. Check if any containers are using the volume
3. Warn you if containers are running
4. Let you select which backup to restore
5. Ask for confirmation before proceeding
6. Clear the volume and restore the selected backup

**Safety features:**
- Checks if volume exists before restoring
- Lists all containers currently using the volume
- Shows container status (running/stopped)
- Requires typing "yes" to confirm restore
- Warns about data loss

### Manual restore (if needed):

If you prefer to restore manually:

```bash
# Stop the container using the volume
docker service scale <service-name>=0

# Extract backup to a temporary location
mkdir -p /tmp/restore
tar -xzf /var/lib/docker-volume-backups/<volume-name>/latest.tar.gz -C /tmp/restore

# Copy to volume using a temporary container
docker run --rm -v <volume-name>:/dest -v /tmp/restore:/source alpine sh -c "cd /source && cp -a . /dest/"

# Start the container
docker service scale <service-name>=1

# Cleanup
rm -rf /tmp/restore
```

## Common Workflows

### Recover from a bad config change

```bash
# SSH to the node
ssh root@dks01.schollar.dev

# List recent backups
volume-backups sonarr_config list

# Restore to a previous backup
volume-backups restore sonarr_config
# Select the backup from before the bad change
# Type 'yes' to confirm
```

### Migrate a service to a different node

```bash
# On source node: Verify latest backup
ssh root@dks01.schollar.dev
volume-backups sonarr_config list

# On destination node: Create volume and restore
ssh root@dks02.schollar.dev

# First, make sure the service creates the volume (start and stop it)
# Or create it manually:
docker volume create --driver linbit/linstor-docker-volume \
  --opt size=1GB --opt fs=xfs --opt replicas=2 --opt storagepool=pool_ssd \
  sonarr_config

# Copy the backup from source node
scp root@dks01.schollar.dev:/var/lib/docker-volume-backups/sonarr_config/latest.tar.gz \
  /var/lib/docker-volume-backups/sonarr_config/

# Restore
volume-backups restore sonarr_config
```

### Check backup health

```bash
# SSH to a node
ssh root@dks01.schollar.dev

# Check last backup time
volume-backups list

# View logs
tail -50 /var/log/linstor-volume-backup.log

# Check systemd timer
systemctl list-timers linstor-volume-backup.timer
```

## Monitoring

The backup script logs all operations to `/var/log/linstor-volume-backup.log`. You can:

- Set up log rotation for this file
- Monitor for backup failures via systemd journal
- Alert on service failures using systemd monitoring tools

### Example: Check for recent backup failures

```bash
sudo journalctl -u linstor-volume-backup.service --since "1 day ago" | grep -i failed
```

## Troubleshooting

### Backup timer not running:

```bash
sudo systemctl status linstor-volume-backup.timer
sudo systemctl enable linstor-volume-backup.timer
sudo systemctl start linstor-volume-backup.timer
```

### No volumes found:

Check that volumes are using the linstor driver:
```bash
docker volume ls --filter driver=linbit/linstor-docker-volume
```

### Permission issues:

Ensure the script is executable:
```bash
sudo chmod +x /usr/local/bin/backup-linstor-volumes.sh
```

### Disk space issues:

Check backup directory size:
```bash
sudo du -sh /var/lib/docker-volume-backups/
```

Reduce retention days in the script if needed.

## Integration with Proxmox Backup Server

Ensure `/var/lib/docker-volume-backups/` is included in your PBS backup job configuration:

1. In Proxmox VE web interface, go to Backup
2. Edit your backup job or create a new one
3. Ensure the backup includes the root filesystem or specifically `/var/lib/`
4. The backups will now be included in your PBS snapshots

## Customization

### Change backup schedule:

Edit `files/linstor-volume-backup.timer` and modify the `OnCalendar` directive:

```ini
# Run every 6 hours
OnCalendar=00/6:00:00

# Run at specific times
OnCalendar=*-*-* 02:00:00,14:00:00
```

Then redeploy:
```bash
ansible-playbook -i ../inventory.yml playbook.yml
```

### Exclude specific volumes:

Modify the backup script to add a filter:

```bash
# Example: Exclude test volumes
LINSTOR_VOLUMES=$(docker volume ls --filter driver=linbit/linstor-docker-volume --format '{{.Name}}' | grep -v 'test')
```

## Security Considerations

- Backups are stored unencrypted; ensure filesystem/PBS encryption is enabled if needed
- Script runs as root; review security hardening options in service file
- Consider implementing backup verification checks
- Restrict access to backup directory: `chmod 700 /var/lib/docker-volume-backups/`
- The volume-backups tool requires root access to manage Docker volumes
