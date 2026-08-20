"""Launcher: primește CloudEvent-ul de la Eventarc și pornește Cloud Run Job-ul.

Există dintr-un motiv de infrastructură, nu de design: Eventarc nu poate ținti
direct un Cloud Run Job (`--destination-run-job` nu există în gcloud 581), doar
un serviciu. Serviciul acesta face un singur lucru, filtrează evenimentul și
declanșează o execuție de job cu obiectul primit, apoi răspunde imediat. Auditul
rulează detașat, în job, exact ca în arhitectura cerută.

Filtrul pe prefixul de ieșire trăiește și aici, nu doar în job: artefactele se
scriu în același bucket, iar un audit care își declanșează propriile ieșiri
înseamnă trei execuții inutile la fiecare rulare.
"""

from __future__ import annotations

import json
import os
from typing import Any

import google.auth
import google.auth.transport.requests
import requests
from fastapi import FastAPI, Request, Response

from job.main import should_process

PROJECT = os.environ["GOOGLE_CLOUD_PROJECT"]
JOB_REGION = os.environ.get("CONSILIUM_JOB_REGION", "europe-west1")
JOB_NAME = os.environ.get("CONSILIUM_JOB_NAME", "consilium-audit")
RUN_ENDPOINT = (
    f"https://run.googleapis.com/v2/projects/{PROJECT}/locations/{JOB_REGION}"
    f"/jobs/{JOB_NAME}:run"
)

app = FastAPI(title="Consilium launcher")


def _target(request_headers: dict[str, str], body: dict[str, Any]) -> tuple[str, str]:
    """Bucket-ul și obiectul, din corpul CloudEvent sau din anteturi."""
    bucket = body.get("bucket") or request_headers.get("ce-bucket", "")
    name = body.get("name") or ""
    if not name:
        subject = request_headers.get("ce-subject", "")
        if subject.startswith("objects/"):
            name = subject[len("objects/") :]
    return bucket, name


def trigger_job(bucket: str, name: str) -> tuple[int, str]:
    """Pornește o execuție de job cu obiectul suprascris în mediu."""
    credentials, _ = google.auth.default(
        scopes=["https://www.googleapis.com/auth/cloud-platform"]
    )
    credentials.refresh(google.auth.transport.requests.Request())
    payload = {
        "overrides": {
            "containerOverrides": [
                {
                    "env": [
                        {"name": "CLOUDEVENT_BUCKET", "value": bucket},
                        {"name": "CLOUDEVENT_NAME", "value": name},
                    ]
                }
            ]
        }
    }
    response = requests.post(
        RUN_ENDPOINT,
        headers={
            "Authorization": f"Bearer {credentials.token}",
            "Content-Type": "application/json",
        },
        data=json.dumps(payload),
        timeout=30,
    )
    return response.status_code, response.text


@app.get("/healthz")
def healthz() -> dict[str, str]:
    return {"status": "ok", "job": JOB_NAME, "region": JOB_REGION}


@app.post("/")
async def receive(request: Request) -> Response:
    headers = {key.lower(): value for key, value in request.headers.items()}
    try:
        body = await request.json()
    except Exception:  # noqa: BLE001 - evenimente în format binar au corp gol
        body = {}

    bucket, name = _target(headers, body if isinstance(body, dict) else {})
    if not bucket or not name:
        print(f"eveniment fără obiect identificabil: {headers.get('ce-subject')}")
        return Response(status_code=204)

    process, reason = should_process(name)
    if not process:
        print(f"ignorat gs://{bucket}/{name}: {reason}")
        return Response(status_code=204)

    status, text = trigger_job(bucket, name)
    print(f"pornit job pentru gs://{bucket}/{name}: HTTP {status}")
    if status >= 400:
        print(text)
        # 204 oricum: o reîncercare Eventarc ar redeclanșa acelasi obiect, iar
        # auditul e idempotent pe audit_id, dar bucla de retry nu ajută.
    return Response(status_code=204)
