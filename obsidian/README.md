# Obsidian Self-Hosted Sync

This stack provides a CouchDB instance for syncing Obsidian vaults using the Self-hosted LiveSync plugin.

## Prerequisites

1. Update the `COUCHDB_PASSWORD` in `docker-compose.yaml` with a strong password.

## Deployment

```bash
docker stack deploy -c docker-compose.yaml obsidian
```

## Setting Up Obsidian LiveSync

### 1. Install the Plugin

In Obsidian:
1. Go to **Settings** → **Community plugins**
2. Disable Safe mode if needed
3. Click **Browse** and search for "Self-hosted LiveSync"
4. Install and enable the plugin

### 2. Configure CouchDB

After deployment, you need to configure CouchDB for single-node mode:

1. Access CouchDB at https://obsidian.schollar.dev/_utils
2. Login with the admin credentials (username: `admin`, password: what you set)
3. Go to **Setup** → **Configure a Single Node**
4. Enter your admin credentials and complete the setup

### 3. Create a Database

1. In CouchDB admin interface, go to **Databases**
2. Create a new database (e.g., `obsidian`)
3. Click on the database and go to **Permissions**
4. Add your admin user to both "Members" and "Admins"

### 4. Configure LiveSync Plugin in Obsidian

In Obsidian, go to Self-hosted LiveSync settings:

1. **Remote Configuration**:
   - **URI**: `https://obsidian.schollar.dev/obsidian` (replace `obsidian` with your database name)
   - **Username**: `admin`
   - **Password**: Your CouchDB password
   - **Database name**: `obsidian` (or whatever you named it)

2. Click **Check database configuration**
3. If successful, click **Initialize database**
4. Enable **LiveSync** and configure sync settings as desired

### 5. Sync Across Devices

Repeat the Obsidian configuration steps on each device where you want to sync your vault.

## Security Recommendations

1. Change the default admin password immediately
2. Consider creating separate CouchDB users for each device/vault
3. Enable HTTPS (already configured via Traefik)
4. Optionally, restrict access using Traefik middleware for IP whitelisting

## Storage Notes

- The CouchDB data volume is set to 50GB by default
- Adjust the size in `docker-compose.yaml` based on your vault size
- CouchDB stores document revisions, so it uses more space than raw file size
