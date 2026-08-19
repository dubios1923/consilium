"""Filtrul care oprește bucla de auto-declanșare.

Artefactele se scriu în același bucket din care se citește. Fără filtrul pe
prefixul de ieșire, fiecare audit ar declanșa alte trei execuții, iar acelea la
rândul lor... Testele astea sunt ieftine și apără de o factură scumpă.
"""

from __future__ import annotations

import pytest

from job.main import should_process


@pytest.mark.parametrize(
    "name",
    [
        "output/aud-abc123/cerere_documente_aud-abc123.pdf",
        "output/aud-abc123/findings.csv",
        "output/audit_report.json",
    ],
)
def test_output_prefix_is_never_reprocessed(name):
    process, reason = should_process(name)
    assert not process
    assert "ieșire" in reason


@pytest.mark.parametrize(
    "name",
    ["zefir12.pdf", "liste/2025-11/zefir12.pdf", "MAJUSCULE.PDF"],
)
def test_incoming_pdfs_are_processed(name):
    assert should_process(name)[0]


@pytest.mark.parametrize(
    "name", ["note.txt", "liste/scan.png", "arhiva.zip", "audit_report.json"]
)
def test_non_pdf_objects_are_ignored(name):
    process, reason = should_process(name)
    assert not process
    assert reason == "nu este PDF"


def test_directory_markers_are_ignored():
    assert not should_process("liste/")[0]


def test_a_file_named_like_the_prefix_elsewhere_is_still_processed():
    """`output/` contează doar ca prefix, nu oriunde în cale."""
    assert should_process("liste/output/zefir12.pdf")[0]


def test_each_audit_gets_its_own_output_directory():
    """Două audituri nu trebuie să-și suprascrie findings.csv reciproc."""
    from consilium.pipeline import artifact_destination_for

    first = artifact_destination_for("gs://bucket/output", "aud-aaa")
    second = artifact_destination_for("gs://bucket/output", "aud-bbb")
    assert first == "gs://bucket/output/aud-aaa"
    assert second == "gs://bucket/output/aud-bbb"
    assert first != second


def test_trailing_slash_does_not_double_up():
    from consilium.pipeline import artifact_destination_for

    assert (
        artifact_destination_for("gs://bucket/output/", "aud-x")
        == "gs://bucket/output/aud-x"
    )


def test_local_destinations_are_also_namespaced():
    from consilium.pipeline import artifact_destination_for

    assert artifact_destination_for("artifacts", "aud-x").endswith("artifacts/aud-x")


def test_artifact_directory_stays_under_the_output_prefix():
    """Altfel artefactele ar cădea în afara filtrului și ar redeclanșa auditul."""
    from consilium.pipeline import artifact_destination_for
    from job.main import should_process

    destination = artifact_destination_for("gs://b/output", "aud-x")
    object_name = destination.removeprefix("gs://b/") + "/findings.csv"
    assert not should_process(object_name)[0]
