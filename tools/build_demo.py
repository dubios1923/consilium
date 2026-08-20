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
CAPTURE_FPS = 3.0
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


def fit_scene(
    shots: Path, frames: int, target_seconds: float, index: int, key: str, work: Path
) -> tuple[Path, float]:
    """Aduce o scena exact la durata replicii ei, dintr-un sir de capturi PNG.

    Sursa e fara pierdere, deci singura limita de calitate e encodarea de aici.
    """
    out = work / f"{index:02d}_{key}.mp4"
    source_fps = frames / target_seconds
    factor = source_fps / CAPTURE_FPS

    filters = [f"fps={FPS}"]
    if factor >= SPEED_BADGE_THRESHOLD and Path(FONT).exists():
        label = f"{factor:.0f}x speed"
        filters.append(
            f"drawtext=fontfile={FONT}:text='{label}':x=w-tw-38:y=38:"
            f"fontsize=22:fontcolor=0xB9BFCA:box=1:boxcolor=0x16181D@0.82:boxborderw=12"
        )

    run([
        "ffmpeg", "-y", "-loglevel", "error",
        "-framerate", f"{source_fps:.6f}", "-i", str(shots / "%05d.png"),
        "-vf", ",".join(filters),
        "-an", "-t", f"{target_seconds:.3f}",
        "-c:v", "libx264", "-preset", "slow", "-crf", "16",
        "-pix_fmt", "yuv420p", str(out),
    ])
    return out, factor


def pad_track(source: Path, target_seconds: float, index: int, work: Path) -> Path:
    """Aduce replica exact la durata segmentului video, adaugand liniste."""
    out = work / f"track_{index:02d}.wav"
    run([
        "ffmpeg", "-y", "-loglevel", "error", "-i", str(source),
        "-af", f"apad=whole_dur={target_seconds:.3f}",
        "-t", f"{target_seconds:.3f}", "-ar", "48000", "-ac", "1", str(out),
    ])
    return out


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", default=str(OUTPUT))
    parser.add_argument("--audio", default=str(AUDIO_MANIFEST))
    parser.add_argument(
        "--score",
        help="pat muzical optional; se aseaza sub voce si se da la o parte "
        "cand se vorbeste",
    )
    args = parser.parse_args()

    audio = json.loads(Path(args.audio).read_text(encoding="utf-8"))
    video = json.loads(VIDEO_MANIFEST.read_text(encoding="utf-8"))
    recorded = {entry["key"]: entry for entry in video["scenes"]}

    work = WORK / Path(args.out).stem
    if work.exists():
        shutil.rmtree(work)
    work.mkdir(parents=True)

    pieces, tracks, total = [], [], 0.0
    for entry in audio["scenes"]:
        key = entry["key"]
        if key not in recorded:
            print(f"  sar peste {key}: nu are înregistrare")
            continue
        item = recorded[key]
        target = max(entry["seconds"], entry["min_seconds"])
        piece, factor = fit_scene(
            Path(item["shots"]), item["frames"], target, entry["index"], key, work
        )
        pieces.append(piece)
        # Pista se completeaza cu liniste pana la durata segmentului video.
        # Fara asta, orice scena cu un minim impus mai lung decat replica ei
        # impinge sunetul inaintea imaginii, iar decalajul se aduna pana la
        # capat: aici ajunsese la zece secunde, iar `-shortest` taia finalul.
        tracks.append(pad_track(Path(entry["audio"]), target, entry["index"], work))
        total += target
        print(f"  {key:18s} {item['recorded_seconds']:6.1f}s -> {target:5.1f}s "
              f"({factor:4.1f}x, {item['frames']} cadre)")

    if not pieces:
        print("nicio scenă de montat")
        return 1

    listing = work / "pieces.txt"
    listing.write_text(
        "".join(f"file '{piece.resolve()}'\n" for piece in pieces), encoding="utf-8"
    )
    silent = work / "silent.mp4"
    run(["ffmpeg", "-y", "-loglevel", "error", "-f", "concat", "-safe", "0",
         "-i", str(listing), "-c", "copy", str(silent)])

    audio_listing = work / "tracks.txt"
    audio_listing.write_text(
        "".join(f"file '{track.resolve()}'\n" for track in tracks), encoding="utf-8"
    )
    narration = work / "narration.wav"
    run(["ffmpeg", "-y", "-loglevel", "error", "-f", "concat", "-safe", "0",
         "-i", str(audio_listing), "-c", "copy", str(narration)])

    if args.score and Path(args.score).is_file():
        # Muzica sta sub voce si e comprimata de ea prin sidechain, deci nu se
        # regleaza volumul de mana pe fiecare replica. `normalize=0` opreste
        # amix sa scada tot mixul cand adauga a doua sursa.
        mixed = work / "mixed.wav"
        run(["ffmpeg", "-y", "-loglevel", "error",
             "-i", str(narration), "-i", str(args.score),
             "-filter_complex", (
                 "[1:a]volume=0.34,afade=t=in:d=3,"
                 f"afade=t=out:st={max(0.0, total - 4):.2f}:d=4[bed];"
                 "[bed][0:a]sidechaincompress=threshold=0.02:ratio=12:"
                 "attack=15:release=350[ducked];"
                 "[ducked][0:a]amix=inputs=2:normalize=0:duration=longest[out]"
             ),
             "-map", "[out]", str(mixed)])
        track = mixed
    else:
        track = narration

    run(["ffmpeg", "-y", "-loglevel", "error",
         "-i", str(silent), "-i", str(track),
         "-c:v", "copy", "-c:a", "aac", "-b:a", "192k", "-ar", "48000", "-ac", "2",
         "-shortest",
         args.out])

    minutes, seconds = divmod(probe_duration(Path(args.out)), 60)
    print(f"\nscris {args.out}  ({int(minutes)}:{seconds:04.1f})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
