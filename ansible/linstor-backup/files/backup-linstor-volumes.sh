#!/bin/bash
set -euo pipefail

# Configuration
BACKUP_DIR="/var/lib/docker-volume-backups"
RETENTION_DAYS=7
LOG_FILE="/var/log/linstor-volume-backup.log"
DATE=$(date +%Y%m%d-%H%M%S)

# Logging function
log() {
    echo "[$(date '+%Y-%m-%d %H:%M:%S')] $1" | tee -a "$LOG_FILE"
}

# Create backup directory if it doesn't exist
mkdir -p "$BACKUP_DIR"

log "Starting linstor volume backup"

# Get list of all volumes using linstor driver (handles both with and without :latest tag)
LINSTOR_VOLUMES=$(docker volume ls --format '{{.Driver}}\t{{.Name}}' | grep -E '^linbit/linstor-docker-volume' | cut -f2)

if [ -z "$LINSTOR_VOLUMES" ]; then
    log "No linstor volumes found"
    exit 0
fi

# Count volumes for reporting
VOLUME_COUNT=$(echo "$LINSTOR_VOLUMES" | wc -l)
log "Found $VOLUME_COUNT linstor volume(s) to backup"

# Backup each volume
SUCCESS_COUNT=0
FAIL_COUNT=0

for VOLUME in $LINSTOR_VOLUMES; do
    log "Backing up volume: $VOLUME"

    # Create volume-specific backup directory
    VOLUME_BACKUP_DIR="$BACKUP_DIR/$VOLUME"
    mkdir -p "$VOLUME_BACKUP_DIR"

    # Backup filename with timestamp
    BACKUP_FILE="$VOLUME_BACKUP_DIR/${VOLUME}_${DATE}.tar.gz"

    BACKUP_SUCCESS=false

    # Check if volume is in use by a running container
    CONTAINER_USING_VOLUME=$(docker ps --filter "volume=$VOLUME" --format '{{.Names}}' | head -1)

    # Method 1: Try backing up from host mount point if volume is in use
    if [ -n "$CONTAINER_USING_VOLUME" ]; then
        log "  Volume in use by container: $CONTAINER_USING_VOLUME"

        # Find where the volume is mounted on the host filesystem
        HOST_MOUNT_POINT=$(mount | grep "$VOLUME" | awk '{print $3}' | head -1)

        if [ -n "$HOST_MOUNT_POINT" ] && [ -d "$HOST_MOUNT_POINT" ]; then
            log "  Attempting backup from host mount point: $HOST_MOUNT_POINT"

            # Create tar directly from the host mount point
            # Use --warning=no-file-changed to handle live databases (like Jellyfin's SQLite)
            # Exit code 0 = success, 1 = files changed but backup completed, 2+ = actual error
            tar --warning=no-file-changed -czf "$BACKUP_FILE" -C "$HOST_MOUNT_POINT" . 2>&1 | tee -a "$LOG_FILE"
            TAR_EXIT_CODE=$?

            if [ $TAR_EXIT_CODE -eq 0 ] || [ $TAR_EXIT_CODE -eq 1 ]; then
                BACKUP_SIZE=$(du -h "$BACKUP_FILE" | cut -f1)
                if [ $TAR_EXIT_CODE -eq 1 ]; then
                    log "✓ Successfully backed up $VOLUME ($BACKUP_SIZE) - files changed during backup (normal for live databases)"
                else
                    log "✓ Successfully backed up $VOLUME ($BACKUP_SIZE)"
                fi
                BACKUP_SUCCESS=true
            else
                log "  ✗ Host mount point backup failed (exit code: $TAR_EXIT_CODE), will try docker mount method"
                # Clean up partial backup file
                rm -f "$BACKUP_FILE"
            fi
        else
            log "  ✗ Could not find valid host mount point, will try docker mount method"
        fi
    fi

    # Method 2: Try mounting the volume directly with docker (if method 1 wasn't attempted or failed)
    if [ "$BACKUP_SUCCESS" = false ]; then
        if [ -n "$CONTAINER_USING_VOLUME" ]; then
            log "  Attempting docker mount method as fallback"
        else
            log "  Volume not in use, attempting docker mount method"
        fi

        # Run docker with timeout to prevent hanging
        timeout 30 docker run --rm \
            -v "$VOLUME:/source:ro" \
            -v "$VOLUME_BACKUP_DIR:/dest" \
            alpine:latest \
            sh -c "cd /source && tar -czf /dest/$(basename "$BACKUP_FILE") ." 2>&1 | tee -a "$LOG_FILE" || true
        DOCKER_EXIT_CODE=$?

        if [ $DOCKER_EXIT_CODE -eq 0 ] && [ -f "$BACKUP_FILE" ]; then
            BACKUP_SIZE=$(du -h "$BACKUP_FILE" | cut -f1)
            log "✓ Successfully backed up $VOLUME ($BACKUP_SIZE)"
            BACKUP_SUCCESS=true
        elif [ $DOCKER_EXIT_CODE -eq 124 ]; then
            log "  ✗ Docker mount method timed out after 30 seconds"
        else
            log "  ✗ Docker mount method failed (exit code: $DOCKER_EXIT_CODE)"
        fi
    fi

    # Final result
    if [ "$BACKUP_SUCCESS" = true ]; then
        SUCCESS_COUNT=$((SUCCESS_COUNT + 1))

        # Create 'latest' symlink
        ln -sf "$(basename "$BACKUP_FILE")" "$VOLUME_BACKUP_DIR/latest.tar.gz"

        # Clean up old backups for this volume (keep last N days)
        find "$VOLUME_BACKUP_DIR" -name "${VOLUME}_*.tar.gz" -mtime +$RETENTION_DAYS -delete 2>&1 | tee -a "$LOG_FILE"
    else
        log "✗ All backup methods failed for $VOLUME"
        FAIL_COUNT=$((FAIL_COUNT + 1))
    fi
done

# Final summary
log "Backup completed: $SUCCESS_COUNT successful, $FAIL_COUNT failed"

# Calculate total backup size
TOTAL_SIZE=$(du -sh "$BACKUP_DIR" | cut -f1)
log "Total backup size: $TOTAL_SIZE"

# Exit with error if any backups failed
if [ $FAIL_COUNT -gt 0 ]; then
    exit 1
fi

exit 0