"""Generează patul muzical al prezentării cu Lyria pe Vertex AI.

Filmul avea patru minute de narațiune peste liniște, ceea ce obosește pe cineva
care se uită la zeci de video-uri la rând. Muzica stă mult sub voce și se dă la o
parte singură când se vorbește; nu e acolo ca să se audă, ci ca să nu se audă
tăcerea.

Lyria întoarce clipuri de ~33 de secunde, deci se generează mai multe, cu
descrieri ușor diferite ca să nu se simtă bucla, și se leagă prin fondu-uri
încrucișate.

Rulare:
    python tools/score.py --seconds 240
"""

from __future__ import annotations

import argparse
import base64
import json
import subprocess
import tempfile
import urllib.request
from pathlib import Path

MODEL = "lyria-002"
LOCATION = "us-central1"
CROSSFADE_SECONDS = 3.0
OUT = Path("demo/audio_o/score.wav")

# Variații pe aceeași atmosferă: destul cât să nu se repete audibil, nu atât
# cât să pară că se schimbă piesa.
PROMPTS = [
    "sparse minimal piano, documentary underscore, calm, unobtrusive, no drums",
    "soft sustained strings under quiet piano, patient, restrained, no percussion",
    "minimal piano with subtle warm pad, contemplative, slow, understated",
    "quiet felt piano, gentle low strings, spacious, no melody in front",
    "restrained ambient piano, soft cello underneath, steady, unhurried",
    "sparse piano motif, airy pad, muted, documentary background, no drums",
    "low warm strings with occasional piano notes, still, patient",
    "minimal contemplative piano, faint texture beneath, quiet ending",
]


def token() -> str:
    import google.auth
    import google.auth.transport.requests

    credentials, _ = google.auth.default(
        scopes=["https://www.googleapis.com/auth/cloud-platform"]
    )
    credentials.refresh(google.auth.transport.requests.Request())
    return credentials.token


def generate(prompt: str, project: str, bearer: str) -> bytes:
    url = (
        f"https://{LOCATION}-aiplatform.googleapis.com/v1/projects/{project}"
        f"/locations/{LOCATION}/publishers/google/models/{MODEL}:predict"
    )
    request = urllib.request.Request(
        url,
        data=json.dumps(
            {"instances": [{"prompt": prompt}], "parameters": {"sample_count": 1}}
        ).encode("utf-8"),
        headers={"Authorization": f"Bearer {bearer}", "Content-Type": "application/json"},
    )
    with urllib.request.urlopen(request, timeout=180) as response:
        payload = json.loads(response.read())
    return base64.b64decode(payload["predictions"][0]["bytesBase64Encoded"])


def duration_of(path: Path) -> float:
    result = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "format=duration",
         "-of", "default=nw=1:nk=1", str(path)],
        capture_output=True, text=True, check=True,
    )
    return float(result.stdout.strip())


def stitch(clips: list[Path], target: float, out: Path) -> None:
    """Leagă clipurile prin fondu-uri încrucișate până se acoperă durata."""
    current = clips[0]
    with tempfile.TemporaryDirectory() as tmp:
        work = Path(tmp)
        for index, nxt in enumerate(clips[1:], start=1):
            merged = work / f"m{index}.wav"
            subprocess.run(
                ["ffmpeg", "-y", "-loglevel", "error", "-i", str(current),
                 "-i", str(nxt), "-filter_complex",
                 f"[0][1]acrossfade=d={CROSSFADE_SECONDS}:c1=tri:c2=tri",
                 str(merged)],
                check=True,
            )
            current = merged
            if duration_of(current) >= target:
                break
        subprocess.run(
            ["ffmpeg", "-y", "-loglevel", "error", "-i", str(current),
             "-t", f"{target:.3f}", "-c", "copy", str(out)],
            check=True,
        )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--seconds", type=float, default=240.0)
    parser.add_argument("--project", default="hoa-agent-ab7x21")
    parser.add_argument("--out", default=str(OUT))
    args = parser.parse_args()

    bearer = token()
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)

    with tempfile.TemporaryDirectory() as tmp:
        work = Path(tmp)
        clips: list[Path] = []
        covered = 0.0
        for index, prompt in enumerate(PROMPTS):
            clip = work / f"{index:02d}.wav"
            clip.write_bytes(generate(prompt, args.project, bearer))
            clips.append(clip)
            covered += duration_of(clip) - CROSSFADE_SECONDS
            print(f"  clip {index + 1}: {duration_of(clip):5.1f}s "
                  f"(acoperit ~{covered:5.1f}s)", flush=True)
            if covered >= args.seconds:
                break
        if covered < args.seconds:
            print(f"atenție: doar {covered:.0f}s din {args.seconds:.0f}s ceruți")
        stitch(clips, min(args.seconds, covered), out)

    print(f"\nscris {out}  ({duration_of(out):.1f}s)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
