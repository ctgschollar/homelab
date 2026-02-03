# Joplin Server

Joplin Server allows you to sync your Joplin notes across devices using your own infrastructure.

## Prerequisites

1. Create a PostgreSQL database for Joplin:
   ```sql
   CREATE DATABASE joplin;
   CREATE USER joplin WITH PASSWORD 'changeme_joplin_db_password';
   GRANT ALL PRIVILEGES ON DATABASE joplin TO joplin;
   ```

2. Update the `POSTGRES_PASSWORD` in `docker-compose.yaml` with your actual password.

## Deployment

```bash
docker stack deploy -c docker-compose.yaml joplin
```

## First-Time Setup

1. Access the Joplin Server at https://joplin.schollar.dev
2. Create an admin account on first login
3. You can then create user accounts for syncing

## Configuring Joplin Clients

In your Joplin desktop/mobile app:
1. Go to **Settings** → **Synchronisation**
2. Select **Joplin Server** as the sync target
3. Enter:
   - **Joplin Server URL**: `https://joplin.schollar.dev`
   - **Email**: Your user email
   - **Password**: Your user password
4. Click **Check synchronisation configuration** to test
5. Start syncing!

## Email Configuration (Optional)

To enable email notifications (e.g., password resets), uncomment and configure the MAILER_* environment variables in `docker-compose.yaml`.
