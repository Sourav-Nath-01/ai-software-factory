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

## Troubleshooting

| Error | Fix |
|---|---|
| `RequestDisallowedByAzure` … "best available regions" | Student subscriptions allow only a few regions. The script lists them when it can; otherwise run `az policy assignment list --disable-scope-strict-match --query "[].{policy:displayName, params:parameters}" -o json` and re-run with `LOCATION=<allowed region>` |
| `SkuNotAvailable` | That VM size is out of capacity in the region: add `VM_SIZE=Standard_B1ms` (or another B-series size) |
| `ResourceNotFound` for `...VMNic` right after VM creation | A CLI glitch in new regions: the VM usually exists. The script now checks and continues; on an older copy, open ports 80/443 with `az vm open-port` |
| Build fails with pip `Read timed out` / `SSL: UNEXPECTED_EOF` while `curl https://pypi.org` works on the VM | Docker's packets are too large for the network path (MTU). cloud-init now sets MTU 1400 in `/etc/docker/daemon.json`; on an older VM write that file, `systemctl restart docker`, and rebuild |
| Site not reachable after 15 minutes | `$RC "tail -n 50 /var/log/cloud-init-output.log"` (see Operate) shows where setup stopped |

## Cost

- Check **Free services** in the portal: Azure free offers have included 750 hours/month of a B1s Linux VM for 12 months. Confirm it applies to your subscription.
- Otherwise a B1s, its disk and a static public IP come to roughly $10–15 per month, so $100 of credit lasts several months.
- Create a budget alert under **Cost Management → Budgets** (for example $10) so a surprise never eats the credit.
- **Delete everything:** `az group delete --name ai-factory-rg`.

## Operate

Cloud Shell sessions are temporary, so the SSH key `provision.sh` generates there disappears with the session. `az vm run-command` needs no key. Set `RC='az vm run-command invoke -g ai-factory-rg -n ai-factory-vm --command-id RunShellScript --query value[0].message -o tsv --scripts'`, then:

| Task | Command |
|---|---|
| Check first-boot setup | `$RC "tail -n 8 /var/log/cloud-init-output.log; docker ps"` |
| Deploy a new version | `$RC "cd /opt/ai-factory && git pull && docker compose -f deploy/azure/compose.yaml up -d --build"` |
| App logs | `$RC "docker compose -f /opt/ai-factory/deploy/azure/compose.yaml logs --tail 50 app"` |
| HTTPS not working yet | `$RC "docker compose -f /opt/ai-factory/deploy/azure/compose.yaml restart caddy"` (Caddy retries the certificate) |
| Health check | `curl https://<fqdn>/api/health` (shows whether the Docker sandbox and analysis tools are available) |

## Design notes (worth knowing for interviews)

- **Docker-outside-of-Docker:** the app container talks to the host's Docker through `/var/run/docker.sock`. The host resolves bind-mount paths, so the output folder is mounted at the **same path** inside and outside the container (`/srv/ai-factory/output`). Otherwise sandbox containers would mount empty folders.
- **The socket is powerful:** access to it is effectively root on the VM. That's acceptable here because the app itself is the only thing with it, and the untrusted code runs in the sandbox containers, never in the app container. A stricter setup would use a rootless Docker daemon or a separate sandbox VM.
- **One instance only:** run state (event queues, approval events, rate-limit buckets) lives in the process, so the app must not be scaled out without a broker such as Redis.
- **Concurrency guard:** `MAX_CONCURRENT_RUNS=2` makes extra runs get HTTP 429 instead of exhausting a 1 GiB VM.
- **No server-side secrets:** demo mode needs no key, and real runs use the visitor's own API key, so the VM holds no credentials worth stealing.
