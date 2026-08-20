"""Generează narațiunea prezentării cu Google Cloud Text-to-Speech.

Un fișier audio per scenă, plus un manifest cu durata fiecăruia. Durata e ce
contează mai departe: montajul întinde sau comprimă imaginea ca să se
potrivească replicii, nu invers. Așa o frază rescrisă nu desincronizează tot.

Rulare:
    python tools/narrate.py --list-voices
    python tools/narrate.py
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import wave
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "demo"))

from storyboard import STORYBOARD

OUT_DIR = Path("demo/audio")
MANIFEST = OUT_DIR / "manifest.json"
DEFAULT_VOICE = "en-US-Neural2-D"
DEFAULT_LANGUAGE = "en-US"
# 48 kHz: Studio genereaza nativ la 24 kHz, deci nu adauga detaliu, dar
# evita artefactele encoderului AAC la rata mica, de unde vine sunetul
# infundat.
SAMPLE_RATE = 48000


def list_voices(language: str) -> None:
    from google.cloud import texttospeech

    client = texttospeech.TextToSpeechClient()
    for voice in client.list_voices(language_code=language).voices:
        if any(tag in voice.name for tag in ("Neural2", "Studio", "Journey", "Wavenet")):
            print(f"  {voice.name:28s} {voice.ssml_gender.name}")


def synthesize(text: str, voice: str, language: str, speaking_rate: float) -> bytes:
    from google.cloud import texttospeech

    client = texttospeech.TextToSpeechClient()
    response = client.synthesize_speech(
        input=texttospeech.SynthesisInput(text=text),
        voice=texttospeech.VoiceSelectionParams(
            language_code=language, name=voice
        ),
        audio_config=texttospeech.AudioConfig(
            audio_encoding=texttospeech.AudioEncoding.LINEAR16,
            sample_rate_hertz=SAMPLE_RATE,
            speaking_rate=speaking_rate,
        ),
    )
    return response.audio_content


def duration_of(path: Path) -> float:
    with wave.open(str(path), "rb") as handle:
        return handle.getnframes() / float(handle.getframerate())


def pad(path: Path, lead: float, tail: float) -> None:
    """Adaugă liniște la început și la sfârșit, ca replicile să nu se lipească."""
    padded = path.with_suffix(".padded.wav")
    subprocess.run(
        [
            "ffmpeg", "-y", "-loglevel", "error",
            "-i", str(path),
            "-af", (
                f"adelay={int(lead * 1000)}|{int(lead * 1000)},"
                f"apad=pad_dur={tail}"
            ),
            str(padded),
        ],
        check=True,
    )
    padded.replace(path)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--voice", default=DEFAULT_VOICE)
    parser.add_argument("--language", default=DEFAULT_LANGUAGE)
    parser.add_argument("--rate", type=float, default=1.0)
    parser.add_argument("--lead", type=float, default=0.18)
    parser.add_argument("--tail", type=float, default=0.30)
    parser.add_argument("--list-voices", action="store_true")
    parser.add_argument(
        "--out-dir",
        default=str(OUT_DIR),
        help="unde se scriu fisierele audio si manifestul",
    )
    args = parser.parse_args()

    if args.list_voices:
        list_voices(args.language)
        return 0

    out_dir = Path(args.out_dir)
    manifest = out_dir / "manifest.json"
    out_dir.mkdir(parents=True, exist_ok=True)
    entries = []
    total = 0.0
    for index, scene in enumerate(STORYBOARD):
        path = out_dir / f"{index:02d}_{scene.key}.wav"
        path.write_bytes(
            synthesize(scene.narration, args.voice, args.language, args.rate)
        )
        pad(path, args.lead, args.tail)
        seconds = duration_of(path)
        total += seconds
        entries.append(
            {
                "index": index,
                "key": scene.key,
                "action": scene.action,
                "audio": str(path),
                "seconds": round(seconds, 3),
                "min_seconds": scene.min_seconds,
            }
        )
        print(f"  {scene.key:18s} {seconds:6.2f}s")

    manifest.write_text(
        json.dumps(
            {"voice": args.voice, "rate": args.rate, "total_seconds": round(total, 2),
             "scenes": entries},
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )
    minutes, seconds = divmod(total, 60)
    print(f"\ntotal narațiune: {int(minutes)}:{seconds:04.1f}")
    print(f"scris {manifest}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
