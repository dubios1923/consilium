"""Montează prezentarea: potrivește fiecare scenă la replica ei, apoi lipește.

Imaginea se adaptează la sunet, nu invers. O scenă de așteptare filmată în două
minute și o replică de nouăsprezece secunde produc o accelerare de șase ori, cu
un indicator pe ecran ca să fie limpede că timpul a fost comprimat, nu că
pipeline-ul e instant.

Rulare:
    python tools/build_demo.py
"""

from __future__ import annotations

import argparse
import json
import shutil
import subprocess
from pathlib import Path

AUDIO_MANIFEST = Path("demo/audio/manifest.json")
VIDEO_MANIFEST = Path("demo/video/manifest.json")
WORK = Path("demo/build")
OUTPUT = Path("demo/consilium_demo.mp4")
FONT = "/usr/share/fonts/liberation-sans-fonts/LiberationSans-Bold.ttf"
FPS = 30
SPEED_BADGE_THRESHOLD = 1.6


def probe_duration(path: Path) -> float:
    result = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "format=duration",
         "-of", "default=nw=1:nk=1", str(path)],
        capture_output=True, text=True, check=True,
    )
    return float(result.stdout.strip())


def run(command: list[str]) -> None:
    subprocess.run(command, check=True, capture_output=True)


def fit_scene(source: Path, target_seconds: float, index: int, key: str) -> Path:
    """Aduce o scenă exact la durata replicii ei."""
    actual = probe_duration(source)
    factor = actual / target_seconds
    out = WORK / f"{index:02d}_{key}.mp4"

    filters = [f"setpts=PTS/{factor:.6f}", f"fps={FPS}"]
    if factor >= SPEED_BADGE_THRESHOLD and Path(FONT).exists():
        label = f"{factor:.0f}x speed"
        filters.append(
            f"drawtext=fontfile={FONT}:text='{label}':x=w-tw-38:y=38:"
            f"fontsize=22:fontcolor=0xB9BFCA:box=1:boxcolor=0x16181D@0.82:boxborderw=12"
        )

    run([
        "ffmpeg", "-y", "-loglevel", "error", "-i", str(source),
        "-vf", ",".join(filters),
        "-an", "-t", f"{target_seconds:.3f}",
        "-c:v", "libx264", "-preset", "medium", "-crf", "20",
        "-pix_fmt", "yuv420p", str(out),
    ])
    return out


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", default=str(OUTPUT))
    args = parser.parse_args()

    audio = json.loads(AUDIO_MANIFEST.read_text(encoding="utf-8"))
    video = json.loads(VIDEO_MANIFEST.read_text(encoding="utf-8"))
    recorded = {entry["key"]: entry for entry in video["scenes"]}

    if WORK.exists():
        shutil.rmtree(WORK)
    WORK.mkdir(parents=True)

    pieces, tracks, total = [], [], 0.0
    for entry in audio["scenes"]:
        key = entry["key"]
        if key not in recorded:
            print(f"  sar peste {key}: nu are înregistrare")
            continue
        source = Path(recorded[key]["video"])
        target = max(entry["seconds"], entry["min_seconds"])
        actual = probe_duration(source)
        piece = fit_scene(source, target, entry["index"], key)
        pieces.append(piece)
        tracks.append(Path(entry["audio"]))
        total += target
        print(f"  {key:18s} {actual:6.1f}s -> {target:5.1f}s "
              f"({actual / target:4.1f}x)")

    if not pieces:
        print("nicio scenă de montat")
        return 1

    listing = WORK / "pieces.txt"
    listing.write_text(
        "".join(f"file '{piece.resolve()}'\n" for piece in pieces), encoding="utf-8"
    )
    silent = WORK / "silent.mp4"
    run(["ffmpeg", "-y", "-loglevel", "error", "-f", "concat", "-safe", "0",
         "-i", str(listing), "-c", "copy", str(silent)])

    audio_listing = WORK / "tracks.txt"
    audio_listing.write_text(
        "".join(f"file '{track.resolve()}'\n" for track in tracks), encoding="utf-8"
    )
    narration = WORK / "narration.wav"
    run(["ffmpeg", "-y", "-loglevel", "error", "-f", "concat", "-safe", "0",
         "-i", str(audio_listing), "-c", "copy", str(narration)])

    run(["ffmpeg", "-y", "-loglevel", "error",
         "-i", str(silent), "-i", str(narration),
         "-c:v", "copy", "-c:a", "aac", "-b:a", "160k", "-shortest",
         args.out])

    minutes, seconds = divmod(probe_duration(Path(args.out)), 60)
    print(f"\nscris {args.out}  ({int(minutes)}:{seconds:04.1f})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
