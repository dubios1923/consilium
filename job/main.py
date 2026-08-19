"""Punctul de intrare al Cloud Run Job-ului declanșat de Eventarc.

Primește evenimentul de obiect finalizat prin variabile de mediu (Eventarc pune
atributele CloudEvent în mediul job-ului), descarcă PDF-ul, rulează pipeline-ul
complet și iese. Nicio intervenție umană între upload și artefactul final.

Ignoră explicit prefixul de ieșire: artefactele scrise de pipeline aterizează în
același bucket, iar fără filtrul ăsta fiecare audit ar declanșa alte trei.
"""

from __future__ import annotations

import asyncio
import hashlib
import os
import sys
import tempfile
from datetime import date
from pathlib import Path

from consilium.config import Config
from consilium.extractor import build_client
from consilium.pipeline import PipelineContext, run_audit
from consilium.state import FirestoreAuditStore

OUTPUT_PREFIX = os.environ.get("CONSILIUM_OUTPUT_PREFIX", "output/")
PDF_SUFFIXES = (".pdf",)


def _event() -> tuple[str, str]:
    """Bucket-ul și obiectul din evenimentul livrat de Eventarc."""
    bucket = os.environ.get("CLOUDEVENT_BUCKET") or os.environ.get("BUCKET")
    name = os.environ.get("CLOUDEVENT_NAME") or os.environ.get("OBJECT")
    if not bucket or not name:
        raise SystemExit(
            "evenimentul nu conține bucket/obiect: setează CLOUDEVENT_BUCKET și "
            "CLOUDEVENT_NAME (Eventarc le injectează automat)"
        )
    return bucket, name


def should_process(object_name: str) -> tuple[bool, str]:
    """Filtrul care oprește bucla de auto-declanșare."""
    if object_name.startswith(OUTPUT_PREFIX):
        return False, f"obiect în prefixul de ieșire `{OUTPUT_PREFIX}`"
    if not object_name.lower().endswith(PDF_SUFFIXES):
        return False, "nu este PDF"
    if object_name.endswith("/"):
        return False, "este un director"
    return True, ""


def main() -> int:
    bucket_name, object_name = _event()
    source_uri = f"gs://{bucket_name}/{object_name}"

    process, reason = should_process(object_name)
    if not process:
        print(f"ignorat {source_uri}: {reason}", flush=True)
        return 0

    from google.cloud import storage

    storage_client = storage.Client()
    blob = storage_client.bucket(bucket_name).blob(object_name)

    with tempfile.TemporaryDirectory() as tmp:
        local = Path(tmp) / Path(object_name).name
        blob.download_to_filename(local)
        content_hash = hashlib.sha256(local.read_bytes()).hexdigest()

        pipeline = PipelineContext(
            store=FirestoreAuditStore(project=os.environ.get("GOOGLE_CLOUD_PROJECT")),
            config=Config.load(os.environ.get("CONSILIUM_CONFIG", "config.yaml")),
            client=build_client(),
            artifact_destination=(
                f"gs://{bucket_name}/{OUTPUT_PREFIX.rstrip('/')}"
            ),
        )
        audit_id = asyncio.run(
            run_audit(
                local,
                source_uri,
                pipeline,
                content_hash=content_hash,
                as_of=date.today(),  # noqa: DTZ011 - termenele legale sunt calendaristice
            )
        )

    record = pipeline.store.get(audit_id)
    status = record.status if record else "necunoscut"
    print(f"audit {audit_id} pentru {source_uri}: {status}", flush=True)
    if record and record.artifact_uris:
        for uri in record.artifact_uris:
            print(f"  artefact: {uri}", flush=True)
    return 0 if status == "done" else 1


if __name__ == "__main__":
    sys.exit(main())
