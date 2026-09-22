#!/bin/bash
set -euo pipefail

PROJECT_ID="your-gcp-project-id"
ZONE="us-central1-a"
INSTANCE_NAME="bill-tracker"
MACHINE_TYPE="e2-micro"
APP_PORT="8080"
YOUR_GOOGLE_ACCOUNT="you@example.com"

gcloud config set project "$PROJECT_ID"

gcloud compute firewall-rules create allow-iap-billtracker \
  --network=default \
  --direction=INGRESS \
  --action=ALLOW \
  --rules="tcp:22,tcp:${APP_PORT}" \
  --source-ranges=35.235.240.0/20 \
  --target-tags=billtracker-iap

gcloud compute instances create "$INSTANCE_NAME" \
  --zone="$ZONE" \
  --machine-type="$MACHINE_TYPE" \
  --image-family=debian-12 \
  --image-project=debian-cloud \
  --tags=billtracker-iap \
  --metadata=enable-oslogin=TRUE \
  --no-address

gcloud projects add-iam-policy-binding "$PROJECT_ID" \
  --member="user:${YOUR_GOOGLE_ACCOUNT}" \
  --role="roles/iap.tunnelResourceAccessor"

gcloud projects add-iam-policy-binding "$PROJECT_ID" \
  --member="user:${YOUR_GOOGLE_ACCOUNT}" \
  --role="roles/compute.osLogin"

echo ""
echo "Done. SSH in with:"
echo "  gcloud compute ssh $INSTANCE_NAME --zone=$ZONE --tunnel-through-iap"
echo ""
echo "See deploy/README.md for deploying the app itself and opening the"
echo "day-to-day access tunnel."
