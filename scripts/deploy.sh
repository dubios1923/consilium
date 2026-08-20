#!/usr/bin/env bash
# Deploy Consilium: Cloud Run Job + launcher + declanșator Eventarc.
#
# Regiunea urmează bucket-ul: un declanșator Eventarc pe Cloud Storage trebuie
# să fie în aceeași locație cu bucket-ul, iar consilium-intake-ab7x21 e în
# europe-west1.
set -euo pipefail

# Scriptul rulează neasistat: `gcloud run deploy --source` cere altfel confirmare
# interactivă pentru crearea repository-ului Artifact Registry și blochează totul.
export CLOUDSDK_CORE_DISABLE_PROMPTS=1

PROJECT="${PROJECT:-hoa-agent-ab7x21}"
REGION="${REGION:-europe-west1}"
BUCKET="${BUCKET:-consilium-intake-ab7x21}"
JOB="${JOB:-consilium-audit}"
LAUNCHER="${LAUNCHER:-consilium-launcher}"
TRIGGER="${TRIGGER:-consilium-intake}"
SA_NAME="${SA_NAME:-consilium-agent}"

# Livrarea pe email e opțională. Se activează doar dacă ambele sunt prezente în
# mediul din care rulezi deploy-ul:
#   CONSILIUM_DELIVERY_TO   destinatarul
#   RESEND_API_KEY          cheia providerului
# Cheia NU intră în specul jobului: ajunge în Secret Manager și e montată ca
# variabilă la runtime. Fără ele, pipeline-ul rulează exact ca înainte.
DELIVERY_TO="${CONSILIUM_DELIVERY_TO:-}"
DELIVERY_FROM="${CONSILIUM_DELIVERY_FROM:-Consilium <consilium@datahappens.ro>}"
DELIVERY_SECRET="${DELIVERY_SECRET:-consilium-delivery-api-key}"
SA="${SA_NAME}@${PROJECT}.iam.gserviceaccount.com"
PROJECT_NUMBER="$(gcloud projects describe "$PROJECT" --format='value(projectNumber)')"

say() { printf '\n\033[1m== %s ==\033[0m\n' "$1"; }

# Un cont de serviciu proaspăt creat nu e imediat vizibil pentru IAM: prima
# legare de rol eșuează cu INVALID_ARGUMENT. Reîncercăm în loc să picăm.
retry() {
  local attempt
  for attempt in 1 2 3 4 5 6; do
    if "$@"; then return 0; fi
    echo "  reîncerc (${attempt}/6) peste 10s: $*"
    sleep 10
  done
  echo "  eșec definitiv: $*" >&2
  return 1
}

say "API-uri"
gcloud services enable \
  run.googleapis.com eventarc.googleapis.com firestore.googleapis.com \
  storage.googleapis.com artifactregistry.googleapis.com \
  cloudbuild.googleapis.com aiplatform.googleapis.com \
  --project="$PROJECT"

say "cont de serviciu"
gcloud iam service-accounts create "$SA_NAME" --project="$PROJECT" \
  --display-name="Consilium pipeline" 2>/dev/null || echo "există deja"

for ROLE in roles/datastore.user roles/aiplatform.user roles/run.invoker \
            roles/eventarc.eventReceiver roles/run.developer; do
  echo "  $ROLE"
  retry gcloud projects add-iam-policy-binding "$PROJECT" \
    --member="serviceAccount:${SA}" --role="$ROLE" --condition=None \
    --quiet >/dev/null
done
retry gcloud storage buckets add-iam-policy-binding "gs://${BUCKET}" \
  --member="serviceAccount:${SA}" --role=roles/storage.objectAdmin \
  --project="$PROJECT" >/dev/null

say "agentul de serviciu Cloud Storage poate publica evenimente"
# `gcloud storage service-agent` întoarce valoarea cu newline și indentare în
# față, ceea ce produce un --member gol. Îl construim din numărul proiectului.
GCS_SA="service-${PROJECT_NUMBER}@gs-project-accounts.iam.gserviceaccount.com"
echo "  $GCS_SA"
retry gcloud projects add-iam-policy-binding "$PROJECT" \
  --member="serviceAccount:${GCS_SA}" --role=roles/pubsub.publisher \
  --condition=None --quiet >/dev/null

DELIVERY_ENV=""
DELIVERY_SECRETS=""
if [ -n "$DELIVERY_TO" ] && [ -n "${RESEND_API_KEY:-}" ]; then
  say "livrare pe email: secret + acces"
  gcloud services enable secretmanager.googleapis.com --project="$PROJECT"
  if ! gcloud secrets describe "$DELIVERY_SECRET" --project="$PROJECT" >/dev/null 2>&1; then
    gcloud secrets create "$DELIVERY_SECRET" --project="$PROJECT" \
      --replication-policy=automatic
  fi
  printf '%s' "$RESEND_API_KEY" | gcloud secrets versions add "$DELIVERY_SECRET" \
    --project="$PROJECT" --data-file=- >/dev/null
  retry gcloud secrets add-iam-policy-binding "$DELIVERY_SECRET" \
    --project="$PROJECT" --member="serviceAccount:${SA}" \
    --role=roles/secretmanager.secretAccessor --quiet >/dev/null
  DELIVERY_ENV=",CONSILIUM_DELIVERY_TO=${DELIVERY_TO},CONSILIUM_DELIVERY_FROM=${DELIVERY_FROM}"
  DELIVERY_SECRETS="--set-secrets=CONSILIUM_DELIVERY_API_KEY=${DELIVERY_SECRET}:latest"
  echo "  destinatar: $DELIVERY_TO"
else
  say "livrare pe email: dezactivată (fără CONSILIUM_DELIVERY_TO / RESEND_API_KEY)"
fi

say "Cloud Run Job (pipeline-ul propriu-zis)"
# GOOGLE_CLOUD_LOCATION ar fi altfel moștenit din --region; Vertex are nevoie de
# `global`, deci îl fixăm explicit.
gcloud run jobs deploy "$JOB" \
  --source . --region="$REGION" --project="$PROJECT" \
  --service-account="$SA" \
  --task-timeout=3600s --max-retries=1 --memory=2Gi --cpu=2 \
  --set-env-vars="GOOGLE_CLOUD_PROJECT=${PROJECT},GOOGLE_GENAI_USE_VERTEXAI=TRUE,GOOGLE_CLOUD_LOCATION=global,CONSILIUM_VERTEX_LOCATION=global,CONSILIUM_OUTPUT_PREFIX=output/,CONSILIUM_ROLE=job${DELIVERY_ENV}" \
  ${DELIVERY_SECRETS}

say "override explicit al locației Vertex"
gcloud run jobs update "$JOB" --region="$REGION" --project="$PROJECT" \
  --update-env-vars="GOOGLE_CLOUD_LOCATION=global,CONSILIUM_VERTEX_LOCATION=global"

say "launcher (serviciul care primește evenimentul)"
gcloud run deploy "$LAUNCHER" \
  --source . --region="$REGION" --project="$PROJECT" \
  --service-account="$SA" --no-allow-unauthenticated \
  --set-env-vars="GOOGLE_CLOUD_PROJECT=${PROJECT},CONSILIUM_ROLE=launcher,CONSILIUM_JOB_NAME=${JOB},CONSILIUM_JOB_REGION=${REGION},CONSILIUM_OUTPUT_PREFIX=output/"

gcloud run services update "$LAUNCHER" --region="$REGION" --project="$PROJECT" \
  --update-env-vars="GOOGLE_CLOUD_LOCATION=global,CONSILIUM_VERTEX_LOCATION=global"

say "agentul de serviciu Eventarc"
# La prima folosire a Eventarc într-un proiect, agentul lui de serviciu nu
# există încă, iar crearea declanșatorului eșuează cu FAILED_PRECONDITION.
# Îl provizionăm explicit; propagarea permisiunilor durează câteva minute.
EVENTARC_SA="service-${PROJECT_NUMBER}@gcp-sa-eventarc.iam.gserviceaccount.com"
gcloud beta services identity create --service=eventarc.googleapis.com \
  --project="$PROJECT" >/dev/null 2>&1 || true
retry gcloud projects add-iam-policy-binding "$PROJECT" \
  --member="serviceAccount:${EVENTARC_SA}" --role=roles/eventarc.serviceAgent \
  --condition=None --quiet >/dev/null

say "declanșator Eventarc"
if gcloud eventarc triggers describe "$TRIGGER" --location="$REGION" \
     --project="$PROJECT" >/dev/null 2>&1; then
  echo "declanșatorul există deja"
else
  # Retry lung: permisiunile agentului Eventarc se propagă în minute, nu secunde.
  for attempt in $(seq 1 20); do
    if gcloud eventarc triggers create "$TRIGGER" \
        --location="$REGION" --project="$PROJECT" \
        --destination-run-service="$LAUNCHER" \
        --destination-run-region="$REGION" \
        --event-filters="type=google.cloud.storage.object.v1.finalized" \
        --event-filters="bucket=${BUCKET}" \
        --service-account="$SA"; then
      break
    fi
    echo "  agentul Eventarc încă nu e gata (${attempt}/20), aștept 30s"
    sleep 30
  done
fi

say "gata"
echo "job     : $JOB ($REGION)"
echo "launcher: $(gcloud run services describe "$LAUNCHER" --region="$REGION" --project="$PROJECT" --format='value(status.url)')"
echo "trigger : $TRIGGER pe gs://${BUCKET}, prefixul output/ ignorat în cod"
