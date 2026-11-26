#!/usr/bin/env bash
# Create an Azure VM that runs AI Software Factory with the full Docker sandbox.
#
# Run in Azure Cloud Shell (bash) — already logged in — or anywhere after `az login`:
#   git clone https://github.com/Sourav-Nath-01/ai-software-factory.git
#   bash ai-software-factory/deploy/azure/provision.sh
#
# Every setting can be overridden: LOCATION=eastus VM_SIZE=Standard_B1ms bash provision.sh
# Delete everything (stops all charges): az group delete --name "$RESOURCE_GROUP"
set -euo pipefail

RESOURCE_GROUP="${RESOURCE_GROUP:-ai-factory-rg}"
LOCATION="${LOCATION:-centralindia}"
VM_NAME="${VM_NAME:-ai-factory-vm}"
VM_SIZE="${VM_SIZE:-Standard_B1s}"
DNS_LABEL="${DNS_LABEL:-ai-factory-$(date +%s | tail -c 6)}"
REPO_URL="${REPO_URL:-https://github.com/Sourav-Nath-01/ai-software-factory.git}"
BRANCH="${BRANCH:-main}"
ADMIN_USER="${ADMIN_USER:-azureuser}"

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
FQDN="$DNS_LABEL.$LOCATION.cloudapp.azure.com"
CLOUD_INIT="$(mktemp)"
sed -e "s|__REPO_URL__|$REPO_URL|g" -e "s|__BRANCH__|$BRANCH|g" -e "s|__FQDN__|$FQDN|g" \
    "$HERE/cloud-init.yaml" > "$CLOUD_INIT"

echo "Creating resource group $RESOURCE_GROUP in $LOCATION ..."
az group create --name "$RESOURCE_GROUP" --location "$LOCATION" --output none

echo "Creating VM $VM_NAME ($VM_SIZE, Ubuntu 24.04) ..."
az vm create \
  --resource-group "$RESOURCE_GROUP" \
  --name "$VM_NAME" \
  --image Ubuntu2404 \
  --size "$VM_SIZE" \
  --admin-username "$ADMIN_USER" \
  --generate-ssh-keys \
  --public-ip-sku Standard \
  --public-ip-address-dns-name "$DNS_LABEL" \
  --os-disk-size-gb 30 \
  --custom-data "$CLOUD_INIT" \
  --output none

echo "Opening ports 80 and 443 ..."
az vm open-port --resource-group "$RESOURCE_GROUP" --name "$VM_NAME" --port 80 --priority 1010 --output none
az vm open-port --resource-group "$RESOURCE_GROUP" --name "$VM_NAME" --port 443 --priority 1020 --output none
rm -f "$CLOUD_INIT"

cat <<EOF

Done. First boot installs Docker and builds the app: allow about 10-15 minutes.

  App:            https://$FQDN
  Watch setup:    ssh $ADMIN_USER@$FQDN 'sudo tail -f /var/log/cloud-init-output.log'
  Update later:   ssh $ADMIN_USER@$FQDN 'cd /opt/ai-factory && sudo git pull && sudo docker compose -f deploy/azure/compose.yaml up -d --build'
  Delete all:     az group delete --name $RESOURCE_GROUP
EOF
