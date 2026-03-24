# Homelab Agent

The Homelab Agent is an autonomous sysadmin agent for Docker Swarm homelabs. It integrates with Slack for user approvals on sensitive operations and provides a safety-first approach to infrastructure automation.

## Prerequisites

Before running the Homelab Agent, you'll need Python 3.12 or later installed on your system. The agent also requires Docker to be available for container management operations. Make sure you have pip or another Python package manager ready to install the project dependencies listed in pyproject.toml.

## Slack App Setup

The agent communicates with Slack through a custom Slack app that you must create and configure in your Slack workspace. This section covers the required setup steps to enable the agent to receive events and handle user interactions.

### Required permissions

Your Slack app must have the following OAuth scopes to function correctly with the agent:
- `chat:write` — to send messages, status updates, and approval requests to Slack channels

This scope is configured in your Slack app's OAuth & Permissions settings. It allows the agent to post messages and update existing messages in your workspace channels.

### Events API configuration

Configure the Request URL in your Slack app's Events API settings to point to `https://<host>/slack/events`. Replace `<host>` with your server's hostname or IP address. The agent exposes this endpoint via FastAPI to receive and process events from Slack in real-time. Events include messages, reactions, and other workspace activities that trigger agent workflows.

### Interactivity configuration

Enable Interactivity in your Slack app settings and configure the Request URL to `https://<host>/slack/interactions`. Replace `<host>` with your server's hostname or IP address. This endpoint handles interactive components like button clicks and modal submissions, allowing users to approve or deny sensitive operations directly from Slack.

### Signing secret

The Slack signing secret is a credential that validates requests from Slack to your agent, ensuring they are genuinely from Slack and not spoofed. This is **required** for the approval listener to bind on a non-loopback interface (i.e., interfaces other than 127.0.0.1). To obtain your signing secret, log into your Slack app dashboard, navigate to the **Basic Information** section, find **App Credentials**, and copy the **Signing Secret** value. Store this value securely and provide it to the agent via the `SLACK_SIGNING_SECRET` environment variable.

## Environment Variables

The agent loads Slack credentials from environment variables rather than directly from the `config.yaml` file. Specifically, `SLACK_BOT_TOKEN` and `SLACK_SIGNING_SECRET` are loaded through the `YamlConfigSettingsSource` mechanism, which allows environment variables to override or supplement configuration file values. This approach provides better security by keeping secrets out of version-controlled configuration files. Set these environment variables before starting the agent:

- `SLACK_BOT_TOKEN` — your Slack app's OAuth token (begins with `xoxb-`)
- `SLACK_SIGNING_SECRET` — the signing secret from your Slack app's credentials

## Running the agent

To run the agent, ensure you have set the required environment variables (`SLACK_BOT_TOKEN` and `SLACK_SIGNING_SECRET`) and that your Slack app is properly configured. Install dependencies using `pip install -e .` from the agent directory, then start the agent with `python cli.py` or the installed `homelab-agent` command. The agent will start a FastAPI web server listening for Slack events and interactivity requests at the configured host and port.
