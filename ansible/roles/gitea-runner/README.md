# gitea-runner Role

Deploys the Gitea act_runner as a systemd service on a dedicated host.

## Prerequisites

1. A Debian 13 VM provisioned and reachable at the IP in `inventory.yml`.
2. SSH access as root using the shared `ansible_ssh_key`.

## Before Running the Playbook

The runner must be registered with a token from Gitea. This is a one-time step.

### 1. Generate a runner token in Gitea

- Log in to Gitea as an admin.
- Go to **Site Administration → Runners → Create Runner Token**.
- Copy the token.

### 2. Place the token on the target host

SSH into the runner VM and write the token to the expected location:

```bash
ssh root@gt-runner-01.home
mkdir -p /etc/gitea-runner
echo 'YOUR_TOKEN_HERE' > /etc/gitea-runner/runner-token
chmod 600 /etc/gitea-runner/runner-token
```

### 3. Run the playbook

From the `ansible/` directory:

```bash
ansible-playbook -i inventory.yml deploy-gitea-runner.yml
```

## Re-registration

If the runner needs to be re-registered (e.g. after a token rotation), delete the state file and re-run the playbook:

```bash
ssh root@gt-runner-01.home rm /opt/act-runner/.runner
ansible-playbook -i inventory.yml deploy-gitea-runner.yml
```

## Defaults

| Variable | Default | Description |
|----------|---------|-------------|
| `gitea_runner_user` | `act-runner` | System user to run the service |
| `gitea_runner_base_dir` | `/opt/act-runner` | Working directory |
| `gitea_runner_config_dir` | `/etc/gitea-runner` | Config and token location |
| `gitea_runner_token_file` | `/etc/gitea-runner/runner-token` | Token file path |
| `gitea_instance_url` | `https://gitea.schollar.dev` | Gitea instance URL |
| `gitea_runner_name` | `{{ inventory_hostname }}` | Runner name shown in Gitea |
| `gitea_runner_labels` | `debian-latest:host,native:host` | Runner labels |
| `gitea_act_runner_version` | `0.2.11` | act_runner binary version |
