# Deploying to Azure (one VM, full sandbox)

One small Linux VM runs the app and Caddy. Caddy serves HTTPS with an automatic Let's Encrypt certificate and proxies the WebSockets.

**Why a VM and not App Service or Container Apps:** the app executes LLM-generated code, and does it safely only inside the Docker sandbox (no network, CPU, memory and process limits). That needs a Docker daemon. Azure's managed container services don't provide one, so there the app would fall back to running generated code unsandboxed on a public server.

```
Internet ──443──► Caddy ──► app (FastAPI + React, one process)
                               │  /var/run/docker.sock
                               ▼
                      sandbox containers (sibling containers on the host's Docker)
```

## Deploy

1. Push this repository to GitHub (the VM clones it).
2. Open **Azure Cloud Shell** (the `>_` icon in the Azure portal), choose **Bash**, and run:

   ```bash
   git clone https://github.com/Sourav-Nath-01/ai-software-factory.git
   bash ai-software-factory/deploy/azure/provision.sh
   ```

3. Wait 10–15 minutes while the VM installs Docker and builds the image, then open the printed `https://<name>.<region>.cloudapp.azure.com` address.

Settings can be overridden: `LOCATION=eastus VM_SIZE=Standard_B1ms BRANCH=main bash .../provision.sh`.

| Variable | Default | Notes |
|---|---|---|
| `LOCATION` | `centralindia` | Student subscriptions may restrict regions; if creation fails with a policy error, try another region |
| `VM_SIZE` | `Standard_B1s` | 1 vCPU, 1 GiB RAM, plus 2 GB swap. `Standard_B1ms` (2 GiB) is smoother if you have the credit |
| `RESOURCE_GROUP` | `ai-factory-rg` | Everything lives here, so deleting it removes all costs |
| `BRANCH` | `main` | Branch the VM clones |

## Cost

- Check **Free services** in the portal: Azure free offers have included 750 hours/month of a B1s Linux VM for 12 months. Confirm it applies to your subscription.
- Otherwise a B1s, its disk and a static public IP come to roughly $10–15 per month, so $100 of credit lasts several months.
- Create a budget alert under **Cost Management → Budgets** (for example $10) so a surprise never eats the credit.
- **Delete everything:** `az group delete --name ai-factory-rg`.

## Operate

| Task | Command |
|---|---|
| Watch first-boot setup | `ssh azureuser@<fqdn> 'sudo tail -f /var/log/cloud-init-output.log'` |
| Deploy a new version | `ssh azureuser@<fqdn> 'cd /opt/ai-factory && sudo git pull && sudo docker compose -f deploy/azure/compose.yaml up -d --build'` |
| App logs | `ssh azureuser@<fqdn> 'sudo docker compose -f /opt/ai-factory/deploy/azure/compose.yaml logs -f app'` |
| Health check | `curl https://<fqdn>/api/health` (shows whether the Docker sandbox and analysis tools are available) |

## Design notes (worth knowing for interviews)

- **Docker-outside-of-Docker:** the app container talks to the host's Docker through `/var/run/docker.sock`. The host resolves bind-mount paths, so the output folder is mounted at the **same path** inside and outside the container (`/srv/ai-factory/output`). Otherwise sandbox containers would mount empty folders.
- **The socket is powerful:** access to it is effectively root on the VM. That's acceptable here because the app itself is the only thing with it, and the untrusted code runs in the sandbox containers, never in the app container. A stricter setup would use a rootless Docker daemon or a separate sandbox VM.
- **One instance only:** run state (event queues, approval events, rate-limit buckets) lives in the process, so the app must not be scaled out without a broker such as Redis.
- **Concurrency guard:** `MAX_CONCURRENT_RUNS=2` makes extra runs get HTTP 429 instead of exhausting a 1 GiB VM.
- **No server-side secrets:** demo mode needs no key, and real runs use the visitor's own API key, so the VM holds no credentials worth stealing.
