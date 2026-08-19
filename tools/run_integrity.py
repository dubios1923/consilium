"""Rulează R0 și recitirea țintită peste o extracție deja salvată.

Separat de extractor ca să se poată itera pe verificare fără să se reia
transcrierea completă (care durează minute bune pe un scan).

Rulare:
    python tools/run_integrity.py samples/extracted/sample_clean_scanned.json \
        samples/synthetic/sample_clean_scanned.pdf
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from consilium.config import Config
from consilium.extractor import build_client, resolve_integrity
from consilium.integrity import check_integrity
from consilium.schema import PaymentList


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("json_path")
    parser.add_argument("pdf_path", nargs="?", help="fără el nu se face recitire")
    parser.add_argument("--config", default="config.yaml")
    parser.add_argument("-o", "--out", help="scrie JSON-ul cu marcajele actualizate")
    args = parser.parse_args()

    config = Config.load(args.config)
    payment = PaymentList.model_validate_json(
        Path(args.json_path).read_text(encoding="utf-8")
    )
    report = check_integrity(payment, config)

    print(f"=== R0 pe {Path(args.json_path).name} ===")
    print(f"rânduri verificate : {report.rows_checked}")
    print(f"coloane verificate : {report.columns_checked}")
    print(f"R0 curat           : {report.is_clean}")
    for issue in report.issues:
        print(f"  [{issue.rule_id}] {issue.message}")
    for cell in report.suspect_cells:
        print(
            f"  celulă localizată  : ap. {cell.apartment_no} × "
            f"„{cell.category}” ({cell.delta:+.2f} lei)"
        )

    client = build_client() if args.pdf_path and not report.is_clean else None
    resolution = resolve_integrity(payment, report, config, args.pdf_path or "", client)

    print("\n--- după recitirea țintită ---")
    print(f"apeluri de recitire        : {resolution.reread_calls}")
    print(f"inconsistențe confirmate   : {len(resolution.confirmed_inconsistencies)}")
    for issue in resolution.confirmed_inconsistencies:
        print(f"    {issue.message}")
    print(f"apartamente neauditabile   : {sorted(resolution.unauditable_apartments)}")
    print(f"categorii neauditabile     : {sorted(resolution.unauditable_categories)}")
    print(f"low_confidence_fields      : {resolution.low_confidence_fields}")
    print(f"document integral auditabil: {resolution.is_fully_auditable}")

    if args.out:
        Path(args.out).write_text(
            json.dumps(payment.model_dump(mode="json"), ensure_ascii=False, indent=2)
            + "\n",
            encoding="utf-8",
        )
        print(f"\nscris {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
