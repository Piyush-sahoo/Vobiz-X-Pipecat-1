#!/usr/bin/env bash
# Optional: deploy to Google Cloud Run.
#
# The primary deployment path is Render (see render.yaml), which deploys on push.
# This script is kept for GCP users. Nothing here is hardcoded — set the values
# in your environment first:
#
#   export GCP_PROJECT=your-project
#   export GCP_REGION=asia-south1
#   export VOBIZ_AUTH_ID=MA_XXXXXXXX
#   export VOBIZ_PHONE_NUMBER=+10000000000
#   ./deploy.sh
#
# Flags that are not defaults, and why:
#   --max-instances=1   active_calls is an in-process dict shared by /answer,
#                       /start, the WebSocket and /initiate-transfer. A second
#                       instance would serve /answer for a call whose WebSocket
#                       lands elsewhere. Cloud Run session affinity is cookie
#                       based and Vobiz's WS client sends no cookie, so one
#                       instance is the only correct setting until that state
#                       moves to Redis/Firestore.
#   --min-instances=1   a cold start on the /answer webhook makes Vobiz time out
#                       mid-call.
#   --no-cpu-throttling Cloud Run throttles CPU between requests by default,
#                       which starves the audio pipeline.
#   --timeout=3600      the 5 minute default would sever the WebSocket mid-call.
#
# NOTE: --allow-unauthenticated silently fails under the org policy
# constraints/iam.allowedPolicyMemberDomains, which rejects allUsers. The deploy
# still exits 0 and prints a URL, but the service returns 403 to everyone.
# Always curl the URL anonymously afterwards to confirm it is actually public.
set -euo pipefail

: "${GCP_PROJECT:?set GCP_PROJECT}"
: "${VOBIZ_AUTH_ID:?set VOBIZ_AUTH_ID}"
: "${VOBIZ_PHONE_NUMBER:?set VOBIZ_PHONE_NUMBER}"
REGION="${GCP_REGION:-asia-south1}"
SERVICE="${SERVICE_NAME:-vobiz-pipecat}"

PROJECT_NUMBER=$(gcloud projects describe "$GCP_PROJECT" --format='value(projectNumber)')
SA="${PROJECT_NUMBER}-compute@developer.gserviceaccount.com"
URL="https://${SERVICE}-${PROJECT_NUMBER}.${REGION}.run.app"

# One-time: let the runtime service account read this service's secrets.
for s in vobiz-pipecat-auth-token vobiz-pipecat-gemini-key; do
  gcloud secrets add-iam-policy-binding "$s" \
    --member="serviceAccount:${SA}" \
    --role=roles/secretmanager.secretAccessor \
    --project="$GCP_PROJECT" --quiet || true
done

gcloud run deploy "$SERVICE" \
  --source . \
  --project="$GCP_PROJECT" \
  --region="$REGION" \
  --allow-unauthenticated \
  --min-instances=1 --max-instances=1 --concurrency=10 \
  --cpu=2 --memory=2Gi \
  --no-cpu-throttling --timeout=3600 \
  --set-env-vars="BOT_MODE=${BOT_MODE:-realtime},ENV=local,VOBIZ_AUTH_ID=${VOBIZ_AUTH_ID},VOBIZ_PHONE_NUMBER=${VOBIZ_PHONE_NUMBER},GEMINI_LIVE_MODEL=${GEMINI_LIVE_MODEL:-models/gemini-3.8-live},GEMINI_VOICE=${GEMINI_VOICE:-Charon},VOBIZ_ENCODING=audio/x-mulaw,VOBIZ_SAMPLE_RATE=8000,ENABLE_RECORDING=true,MAX_RECORDING_LENGTH=3600,PUBLIC_URL=${URL}" \
  --set-secrets="VOBIZ_AUTH_TOKEN=vobiz-pipecat-auth-token:latest,GOOGLE_API_KEY=vobiz-pipecat-gemini-key:latest" \
  --quiet

echo
echo "Deployed to ${URL}"
echo "Confirm it is genuinely public (must NOT be 403):"
echo "  curl -s -o /dev/null -w '%{http_code}\\n' ${URL}/dashboard"
