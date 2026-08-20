"""Compara JSON-ul extras cu adevarul de referinta produs de generator.

Generatorul stie exact ce numar a tiparit in fiecare celula, deci putem masura
fidelitatea transcrierii camp cu camp, nu doar "pare corect".

Rulare:
    python tools/check_extraction.py samples/extracted/sample_clean.json
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from gen_samples import CATEGORIES, SHOW_CONSUMPTION, build_document

LABEL_BY_KEY = {key: label for key, label, _ in CATEGORIES}
TOLERANCE = 0.005


def ground_truth(variant: str) -> dict:
    """Reconstruieste documentul in forma schemei publice."""
    document, _ = build_document(variant)
    show_consumption = SHOW_CONSUMPTION[variant]

    apartments = []
    for apartment in document["apartment_lines"]:
        entry = {
            "apartment_no": apartment["apartment_no"],
            "persons": apartment["persons"],
            "cota_indiviza": apartment["cota_indiviza"],
            "charges": {
                LABEL_BY_KEY[key]: value
                for key, value in apartment["charges"].items()
            },
            "arrears": apartment["arrears"],
            "penalties": apartment["penalties"],
            "total_due": apartment["total_due"],
            "consumption": (
                {
                    "apa_rece_mc": apartment["m3_apa_rece"],
                    "apa_calda_mc": apartment["m3_apa_calda"],
                    "caldura_gcal": None,
                }
                if show_consumption
                else None
            ),
        }
        apartments.append(entry)

    return {
        "document_id": document["document_id"],
        "association_ref": document["association_ref"],
        "period": document["period"],
        "declared_totals": document["declared_totals"],
        "expense_lines": [
            {
                "category": line["category"],
                "amount": line["amount"],
                "distribution_key": line["distribution_key"],
                "source_invoice_ref": line["source_invoice_ref"],
            }
            for line in document["expense_lines"]
        ],
        "apartment_lines": apartments,
    }


def compare(path: str, expected: object, found: object, out: list[tuple]) -> int:
    """Compara recursiv si aduna nepotrivirile. Intoarce numarul de campuri."""
    if isinstance(expected, dict):
        if not isinstance(found, dict):
            out.append((path, expected, found))
            return 1
        count = 0
        for key in expected:
            count += compare(f"{path}.{key}", expected[key], found.get(key), out)
        for key in found:
            if key not in expected:
                out.append((f"{path}.{key}", "<absent in referinta>", found[key]))
                count += 1
        return count
    if isinstance(expected, list):
        if not isinstance(found, list):
            out.append((path, f"<lista de {len(expected)}>", found))
            return 1
        if len(expected) != len(found):
            out.append((path + ".<lungime>", len(expected), len(found)))
        count = 0
        for index in range(max(len(expected), len(found))):
            item_expected = expected[index] if index < len(expected) else None
            item_found = found[index] if index < len(found) else None
            count += compare(f"{path}[{index}]", item_expected, item_found, out)
        return count
    if isinstance(expected, (int, float)) and isinstance(found, (int, float)):
        if abs(float(expected) - float(found)) > TOLERANCE:
            out.append((path, expected, found))
        return 1
    if expected != found:
        out.append((path, expected, found))
    return 1


def canonical_path(path: str) -> str:
    """Forma comparabila a unei cai de camp.

    Nepotrivirile sunt raportate ca `.apartment_lines[3].charges.Apă rece`, iar
    extractorul marcheaza `apartment_lines[3].charges['Apă rece']`. Le aduc pe
    amandoua la aceeasi forma ca sa pot masura acoperirea.
    """
    cleaned = path.strip().lstrip(".")
    for character in "['\"]":
        cleaned = cleaned.replace(character, ".")
    while ".." in cleaned:
        cleaned = cleaned.replace("..", ".")
    return cleaned.strip(".").casefold()


def coverage(
    mismatches: list[tuple], flagged: list[str]
) -> tuple[list[str], list[str], list[str]]:
    """Imparte erorile in semnalate / nesemnalate si marcajele in utile / prudente.

    Un marcaj acopera o eroare daca una dintre cai este prefix al celeilalte:
    marcarea unui rand intreg acopera si celulele lui.
    """
    error_paths = [canonical_path(field) for field, _, _ in mismatches]
    flag_paths = [canonical_path(field) for field in flagged]

    def covers(flag: str, error: str) -> bool:
        return flag == error or error.startswith(flag + ".") or flag.startswith(
            error + "."
        )

    signalled = [
        error for error in error_paths if any(covers(f, error) for f in flag_paths)
    ]
    silent = [error for error in error_paths if error not in signalled]
    unused = [
        flag for flag in flag_paths if not any(covers(flag, e) for e in error_paths)
    ]
    return signalled, silent, unused


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("json_path")
    parser.add_argument(
        "--variant",
        help="clean|errors|penalties (implicit: dedus din numele fisierului)",
    )
    args = parser.parse_args()

    path = Path(args.json_path)
    variant = (
        args.variant
        or path.stem.replace("sample_", "").replace("_scanned", "").replace("_alt", "")
    )
    extracted = json.loads(path.read_text(encoding="utf-8"))
    expected = ground_truth(variant)

    # Ordinea pozitiilor de cheltuiala e o alegere de layout, nu un fapt despre
    # document: reconciler-ul le leaga dupa `category`, nu dupa index. Comparam
    # dupa categorie, altfel un layout care reordoneaza tabelul apare ca 29 de
    # erori de transcriere care nu exista.
    def by_category(lines: list) -> dict:
        return {line["category"]: line for line in lines}

    found = {key: extracted.get(key) for key in expected}
    if isinstance(found.get("expense_lines"), list):
        expected = dict(expected, expense_lines=by_category(expected["expense_lines"]))
        found["expense_lines"] = by_category(found["expense_lines"])

    mismatches: list[tuple] = []
    total = compare("", expected, found, mismatches)

    label = variant
    if "_scanned" in path.stem:
        label += " scanat"
    if "_alt" in path.stem:
        label += " layout alternativ"
    print(f"=== {path.name} (varianta: {label}) ===")
    print(f"campuri comparate : {total}")
    print(f"nepotriviri       : {len(mismatches)}")
    accuracy = 100.0 * (total - len(mismatches)) / total if total else 0.0
    print(f"fidelitate        : {accuracy:.3f}%")

    flagged = extracted.get("extraction_confidence", {}).get(
        "low_confidence_fields", []
    )
    print(f"low_confidence    : {len(flagged)} {flagged if flagged else ''}")

    signalled, silent, unused = coverage(mismatches, flagged)
    print("\n--- acoperirea marcajelor de încredere ---")
    print(f"erori semnalate ca low-confidence : {len(signalled)}")
    print(f"erori NESEMNALATE (tăcute)        : {len(silent)}")
    print(f"marcaje pe câmpuri corecte        : {len(unused)}")
    if silent:
        print("  câmpuri greșite și nemarcate:")
        for field in silent[:15]:
            print(f"    {field}")
        if len(silent) > 15:
            print(f"    ... încă {len(silent) - 15}")

    if mismatches:
        print("\n--- nepotriviri ---")
        for field, want, got in mismatches[:60]:
            print(f"  {field}\n      asteptat: {want!r}\n      gasit   : {got!r}")
        if len(mismatches) > 60:
            print(f"  ... inca {len(mismatches) - 60}")
    return 1 if mismatches else 0


if __name__ == "__main__":
    raise SystemExit(main())
