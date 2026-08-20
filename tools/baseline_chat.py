"""Benchmark: ce obții dacă dai PDF-ul direct unui chat, într-un singur apel.

Întrebarea la care răspunde: merită arhitectura, sau ajunge un prompt bun?

Trimite documentul la același model pe care îl folosește extractorul, cu
promptul pe care l-ar scrie un om, și compară rezultatul cu erorile plantate
documentate în expected_findings.json. Rulează de mai multe ori pe același
input, la temperatura implicită, pentru că variația între rulări e ea însăși un
rezultat.

Măsoară patru lucruri:
  - constatări plantate găsite (recall)
  - constatări inventate (fals pozitive)
  - cifre din scrisoare care nu apar nicăieri în document
  - variație între rulări pe același input

Rulare:
    python tools/baseline_chat.py --runs 5
    python tools/baseline_chat.py --samples sample_clean_scanned --runs 5
"""

from __future__ import annotations

import argparse
import json
import re
import statistics
import subprocess
import sys
from collections import Counter
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from consilium.extractor import MODEL, build_client

SYNTHETIC = Path("samples/synthetic")

PROMPT = """\
Verifică această listă de plată, găsește erorile și redactează o cerere formală
către asociație."""

# Sumele din raspuns, in format romanesc. Aceleasi tipare ca la validatorul
# scrisorii, ca sa comparam masurat cu masurat.
MONEY = re.compile(r"\d{1,3}(?:\.\d{3})*,\d{2}|\d+,\d{2}")

# Reguli pe care le poate „gasi" un raspuns liber, dupa cuvintele cheie folosite.
RULE_MARKERS = {
    "R1": ("total general", "totalul general", "total declarat"),
    "R2": ("cot", "indiviz"),
    "R3": ("repartiz", "cheia", "cheie de repartizare"),
    "R4": ("penaliz", "0,2", "0.2"),
    "R5": ("suma cheltuielilor", "pe apartamente", "repartizat efectiv"),
}


def pdf_text(path: Path) -> str:
    """Textul documentului, pentru a verifica daca o cifra chiar exista in el."""
    result = subprocess.run(
        ["pdftotext", "-layout", str(path), "-"],
        capture_output=True,
        text=True,
        check=False,
    )
    return result.stdout


def normalize(token: str) -> str:
    return token.replace(".", "").replace(",", ".")


def amounts_in_document(path: Path) -> set[str]:
    text = pdf_text(path)
    found = {normalize(token) for token in MONEY.findall(text)}
    # Documentele scanate nu au strat de text; fara el nu putem judeca cifrele.
    return found


def planted_rules(expected: dict, sample_pdf: str) -> list[str]:
    return [
        finding["rule_id"]
        for finding in expected["findings"]
        if finding["sample"] == sample_pdf
        and finding["type"] != "unverifiable_distribution"
    ]


def rules_claimed(answer: str) -> set[str]:
    """Ce reguli pare sa fi atins raspunsul, dupa vocabular."""
    lowered = answer.lower()
    return {
        rule
        for rule, markers in RULE_MARKERS.items()
        if any(marker in lowered for marker in markers)
    }


def run_once(client: Any, pdf: Path) -> str:
    from google.genai import types

    response = client.models.generate_content(
        model=MODEL,
        contents=[
            types.Content(
                role="user",
                parts=[
                    types.Part.from_bytes(
                        data=pdf.read_bytes(), mime_type="application/pdf"
                    ),
                    types.Part.from_text(text=PROMPT),
                ],
            )
        ],
    )
    return response.text or ""


def evaluate(answer: str, pdf: Path, planted: list[str]) -> dict[str, Any]:
    claimed = rules_claimed(answer)
    expected = set(planted)
    document_amounts = amounts_in_document(pdf)
    quoted = [normalize(token) for token in MONEY.findall(answer)]
    ungrounded = [value for value in quoted if value not in document_amounts]
    return {
        "found": sorted(expected & claimed),
        "missed": sorted(expected - claimed),
        "extra": sorted(claimed - expected),
        "amounts_quoted": len(quoted),
        "amounts_ungrounded": len(ungrounded),
        "ungrounded_sample": sorted(set(ungrounded))[:6],
        "answer_chars": len(answer),
    }


def summarize(name: str, results: list[dict[str, Any]], planted: list[str]) -> None:
    print(f"\n=== {name} · {len(results)} rulări · plantate: {planted or 'niciuna'} ===")
    header = f"{'#':>2}  {'găsite':<14} {'ratate':<14} {'inventate':<12} {'cifre':>6} {'nefondate':>10}"
    print(header)
    print("-" * len(header))
    for index, item in enumerate(results, start=1):
        print(
            f"{index:>2}  {','.join(item['found']) or '-':<14} "
            f"{','.join(item['missed']) or '-':<14} "
            f"{','.join(item['extra']) or '-':<12} "
            f"{item['amounts_quoted']:>6} {item['amounts_ungrounded']:>10}"
        )
    recalls = [len(r["found"]) for r in results]
    extras = [len(r["extra"]) for r in results]
    ungrounded = [r["amounts_ungrounded"] for r in results]
    total = len(planted)
    print(
        f"    recall mediu {statistics.mean(recalls):.1f}/{total}"
        f"  ·  fals pozitive medii {statistics.mean(extras):.1f}"
        f"  ·  cifre nefondate medii {statistics.mean(ungrounded):.1f}"
    )
    distinct = Counter(tuple(r["found"]) for r in results)
    print(f"    seturi distincte de constatări între rulări: {len(distinct)}")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--runs", type=int, default=5)
    parser.add_argument(
        "--samples",
        nargs="*",
        default=["sample_errors", "sample_penalties", "sample_clean_scanned"],
    )
    parser.add_argument("--out", default="samples/baseline_chat.json")
    args = parser.parse_args()

    expected = json.loads(
        (SYNTHETIC / "expected_findings.json").read_text(encoding="utf-8")
    )
    client = build_client()
    everything: dict[str, Any] = {"model": MODEL, "prompt": PROMPT, "runs": {}}

    for name in args.samples:
        pdf = SYNTHETIC / f"{name}.pdf"
        if not pdf.is_file():
            print(f"lipsește {pdf}", file=sys.stderr)
            continue
        source = f"{name.replace('_scanned', '').replace('_alt', '')}.pdf"
        planted = planted_rules(expected, source)

        results = []
        for index in range(args.runs):
            print(f"  {name}: rularea {index + 1}/{args.runs}", file=sys.stderr)
            try:
                answer = run_once(client, pdf)
            except Exception as error:  # noqa: BLE001
                print(f"    eșec: {error}", file=sys.stderr)
                continue
            item = evaluate(answer, pdf, planted)
            item["answer"] = answer
            results.append(item)

        everything["runs"][name] = {"planted": planted, "results": results}
        summarize(name, results, planted)

    Path(args.out).write_text(
        json.dumps(everything, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print(f"\nscris {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
