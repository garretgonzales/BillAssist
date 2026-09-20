#!/bin/bash
# One-time GCP setup for running Bill Tracker on a Compute Engine VM with
# no public IP at all - the only way in is an IAP tunnel authenticated to
# your own Google account. Review every value below before running.
#
# This creates BILLED infrastructure (a running VM, ~$13-15/mo for
# e2-small, less for e2-micro - see MACHINE_TYPE below). Nothing here is
# free indefinitely. Run this yourself after reading it; it's not
# something to pipe into a shell blindly.
set -euo pipefail

PROJECT_ID="your-gcp-project-id"
ZONE="us-central1-a"
INSTANCE_NAME="bill-tracker"
MACHINE_TYPE="e2-micro"               # free-tier eligible in us-central1
APP_PORT="8080"
YOUR_GOOGLE_ACCOUNT="you@example.com"

gcloud config set project "$PROJECT_ID"

# 1. Firewall: allow only IAP's own fixed range to reach SSH + the app
#    port. 35.235.240.0/20 is Google's documented IAP range, not your IP -
#    don't substitute your own address here.
gcloud compute firewall-rules create allow-iap-billtracker \
  --network=default \
  --direction=INGRESS \
  --action=ALLOW \
  --rules="tcp:22,tcp:${APP_PORT}" \
  --source-ranges=35.235.240.0/20 \
  --target-tags=billtracker-iap

# 2. The VM. --no-address means no public IP whatsoever - IAP tunnels to
#    the internal IP directly, so there's nothing internet-facing to scan
#    or attack. OS Login lets IAP-based SSH use your Google identity
#    instead of you managing SSH keys by hand.
gcloud compute instances create "$INSTANCE_NAME" \
  --zone="$ZONE" \
  --machine-type="$MACHINE_TYPE" \
  --image-family=debian-12 \
  --image-project=debian-cloud \
  --tags=billtracker-iap \
  --metadata=enable-oslogin=TRUE \
  --no-address

# 3. Grant yourself permission to open IAP tunnels to this project, and to
#    SSH in via OS Login. Narrow this to your own account only - anyone
#    else with these roles could also reach the VM through IAP.
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
