"""Înregistrează prezentarea: Playwright conduce pagina, uploadurile sunt reale.

Nu capturează desktopul. Chromium rulează headless și Playwright scrie direct
video-ul contextului, deci nu e nevoie de portalul Wayland și nu apare niciun
dialog de permisiune. Se poate rerula oricând, inclusiv din CI.

Un context per scenă, deci un fișier video per scenă. Așa nu trebuie tăiat nimic
după: montajul doar potrivește viteza fiecărei bucăți la replica ei.

Rulare:
    python tools/record_demo.py
    python tools/record_demo.py --dashboard http://localhost:8099
"""

from __future__ import annotations

import argparse
import base64
import json
import subprocess
import sys
import tempfile
import time
import urllib.request
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "demo"))

from storyboard import STORYBOARD, Scene

DASHBOARD = "https://consilium-dashboard-aq2ftfgfkq-ew.a.run.app"
BUCKET = "gs://consilium-intake-ab7x21"
SAMPLES = Path("samples/synthetic")
OUT_DIR = Path("demo/video")
MANIFEST = OUT_DIR / "manifest.json"
VIEWPORT = {"width": 1440, "height": 900}
WAIT_LIMIT_SECONDS = 240

# Capturi PNG in loc de inregistrare video. Playwright scrie VP8 la ~500 kbps,
# ceea ce face textul din terminal pasta la orice miscare; sursa devine astfel
# plafonul calitatii, indiferent cat de generos e exportul final. Cu capturi,
# sursa e fara pierdere si singura limita ramane encodarea noastra.
CAPTURE_FPS = 3.0

CARD_CSS = """
* { box-sizing: border-box; }
body { margin:0; height:100vh; display:flex; flex-direction:column;
       justify-content:center; padding:0 9vw; background:#101216; color:#e8eaee;
       font:16px/1.6 -apple-system,"Segoe UI",Roboto,"Liberation Sans",sans-serif; }
h1 { font-size:44px; line-height:1.15; letter-spacing:-0.02em; margin:0 0 34px;
     max-width:20ch; }
ul { list-style:none; margin:0; padding:0; }
li { font-size:22px; line-height:1.5; color:#b9bfca; margin-bottom:14px;
     padding-left:22px; position:relative; }
li:before { content:""; position:absolute; left:0; top:13px; width:9px; height:2px;
            background:#6fb39a; }
.mark { position:absolute; bottom:6vh; left:9vw; font-size:14px; color:#61656e;
        letter-spacing:0.14em; text-transform:uppercase; }
"""


def card_html(scene: Scene) -> str:
    items = "".join(f"<li>{line}</li>" for line in scene.card_lines)
    return (
        f"<!doctype html><html><head><meta charset='utf-8'>"
        f"<style>{CARD_CSS}</style></head><body>"
        f"<h1>{scene.card_title}</h1><ul>{items}</ul>"
        f"<div class='mark'>Consilium</div></body></html>"
    )


LETTER_CSS = """
* { box-sizing:border-box; }
body { margin:0; min-height:100vh; background:#101216; padding:34px 0;
       display:flex; gap:26px; justify-content:center; align-items:flex-start; }
img { width:44%; max-width:560px; border-radius:6px;
      box-shadow:0 18px 50px rgba(0,0,0,.65); }
"""


def letter_html(pdf: bytes, pages: int = 2) -> str:
    """Randeaza scrisoarea ca imagini si o aseaza intr-o pagina.

    Chromium headless nu afiseaza PDF-uri: `page.goto` catre un PDF declanseaza
    o descarcare si arunca. Randam noi paginile, ceea ce da si control asupra
    incadraturii.
    """
    with tempfile.TemporaryDirectory() as tmp:
        source = Path(tmp) / "letter.pdf"
        source.write_bytes(pdf)
        subprocess.run(
            ["pdftoppm", "-r", "150", "-png", "-f", "1", "-l", str(pages),
             str(source), str(Path(tmp) / "p")],
            check=True, capture_output=True,
        )
        images = sorted(Path(tmp).glob("p-*.png"))
        tags = "".join(
            f'<img src="data:image/png;base64,'
            f'{base64.b64encode(image.read_bytes()).decode()}">'
            for image in images
        )
    return (
        f"<!doctype html><html><head><meta charset='utf-8'>"
        f"<style>{LETTER_CSS}</style></head><body>{tags}</body></html>"
    )


TERMINAL_CSS = """
* { box-sizing:border-box; }
body { margin:0; min-height:100vh; background:#0b0d10; padding:44px 56px;
       font:15.5px/1.65 "Liberation Mono","DejaVu Sans Mono",monospace;
       color:#c8cdd6; }
.win { background:#14171c; border:1px solid #262b33; border-radius:10px;
       overflow:hidden; box-shadow:0 22px 60px rgba(0,0,0,.6); }
.bar { background:#1b1f26; padding:9px 14px; border-bottom:1px solid #262b33;
       font-size:12.5px; color:#7d8492; letter-spacing:.04em; }
.body { padding:20px 22px; white-space:pre-wrap; word-break:break-word; }
.cmd { color:#e8eaee; }
.cmd .p { color:#6fb39a; }
.out { color:#98a0ad; }
"""


def terminal_html(blocks: list[tuple[str, str]]) -> str:
    """Fereastra de terminal cu comenzile si output-ul lor, exact cum au rulat."""
    parts = []
    for command, output in blocks:
        parts.append(
            f'<div class="cmd"><span class="p">$</span> {command}</div>'
            f'<div class="out">{output}</div>'
        )
    return (
        f"<!doctype html><html><head><meta charset='utf-8'>"
        f"<style>{TERMINAL_CSS}</style></head><body>"
        f"<div class='win'><div class='bar'>consilium</div>"
        f"<div class='body'>{''.join(parts)}</div></div></body></html>"
    )


def run_and_capture(command: list[str]) -> tuple[str, str]:
    """Ruleaza comanda si intoarce (text afisat, output). Nimic pregatit."""
    result = subprocess.run(command, capture_output=True, text=True, check=False)
    output = (result.stdout or result.stderr or "").strip()
    shown = " ".join(command)
    return shown, output


def upload(sample: str) -> str:
    """Încarcă un sample cu nume unic. Întoarce numele obiectului."""
    name = f"demo_{int(time.time())}_{sample}"
    subprocess.run(
        ["gcloud", "storage", "cp", str(SAMPLES / sample), f"{BUCKET}/liste/{name}"],
        check=True,
        capture_output=True,
    )
    return name


def audit_for(source_name: str) -> dict[str, Any] | None:
    from consilium.state import FirestoreAuditStore

    for record in FirestoreAuditStore(project="hoa-agent-ab7x21").list_recent(limit=10):
        if record.source_uri.endswith(source_name):
            return {"id": record.audit_id, "status": record.status}
    return None


class Shots:
    """Tine cadrul si captureaza, in loc sa doarma."""

    def __init__(self, page: Any, directory: Path) -> None:
        self.page = page
        self.directory = directory
        self.count = 0
        self.failures = 0
        directory.mkdir(parents=True, exist_ok=True)

    def shoot(self) -> bool:
        """Un cadru. Un esec nu opreste filmarea: la 3 fps nu se vede lipsa lui."""
        try:
            self.page.screenshot(
                path=str(self.directory / f"{self.count + 1:05d}.png"),
                animations="disabled",
            )
        except Exception as error:  # noqa: BLE001
            self.failures += 1
            if self.failures in (1, 10):
                print(f"    captura esuata ({error!s:.60}...)", flush=True)
            return False
        self.count += 1
        return True

    def hold(self, seconds: float) -> None:
        deadline = time.time() + max(0.0, seconds)
        interval = 1.0 / CAPTURE_FPS
        while True:
            began = time.time()
            self.shoot()
            if time.time() >= deadline:
                return
            time.sleep(max(0.0, interval - (time.time() - began)))


def record_scene(
    browser: Any, scene: Scene, seconds: float, state: dict[str, Any], dashboard: str
) -> Any:
    """Capturează o scenă într-un context propriu. Întoarce setul de capturi."""
    context = browser.new_context(
        viewport=VIEWPORT, device_scale_factor=1, color_scheme="dark"
    )
    page = context.new_page()
    shots = Shots(page, OUT_DIR / "shots" / scene.key)
    hold = shots.hold
    try:
        if scene.action == "card":
            page.set_content(card_html(scene))
            hold(seconds)

        elif scene.action == "upload":
            name = f"demo_{int(time.time())}_{scene.sample}"
            command = [
                "gcloud", "storage", "cp",
                str(SAMPLES / scene.sample), f"{BUCKET}/liste/{name}",
            ]
            page.set_content(terminal_html([(" ".join(command), "")]))
            hold(min(2.5, seconds * 0.35))
            shown, output = run_and_capture(command)
            page.set_content(
                terminal_html([(shown, output or "Copying... done.")])
            )
            state[scene.sample] = name
            hold(max(1.5, seconds - min(2.5, seconds * 0.35)))

        elif scene.action == "wait_state":
            deadline = time.time() + WAIT_LIMIT_SECONDS
            started = time.time()
            source = state.get("current")
            while time.time() < deadline:
                page.goto(dashboard, wait_until="networkidle")
                found = audit_for(source) if source else None
                if found and found["status"] == scene.target_status:
                    state["last_audit"] = found["id"]
                    break
                hold(4.0)
            hold(max(0.0, seconds - (time.time() - started)))

        elif scene.action == "detail":
            page.goto(
                f"{dashboard}/audit/{state['last_audit']}", wait_until="networkidle"
            )
            if scene.scroll_to:
                page.mouse.wheel(0, scene.scroll_to)
            hold(seconds)

        elif scene.action == "terminal":
            blocks: list[tuple[str, str]] = []
            share = seconds / max(1, len(scene.commands))
            for command in scene.commands:
                blocks.append(run_and_capture(command))
                page.set_content(terminal_html(blocks))
                hold(share)

        elif scene.action == "letter":
            url = f"{dashboard}/audit/{state['last_audit']}/letter"
            with urllib.request.urlopen(url, timeout=30) as response:
                pdf = response.read()
            page.set_content(letter_html(pdf))
            hold(seconds)

        else:
            raise ValueError(f"acțiune necunoscută: {scene.action}")
    finally:
        context.close()
    if shots.count == 0:
        raise RuntimeError(f"nicio captură pentru scena {scene.key}")
    return shots


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dashboard", default=DASHBOARD)
    parser.add_argument("--only", nargs="*", help="doar scenele cu aceste chei")
    parser.add_argument(
        "--audit-id",
        help="dosarul folosit de scenele de detaliu, ca sa se poata relua "
        "coada fara sa se refaca uploadurile",
    )
    args = parser.parse_args()

    audio = json.loads(Path("demo/audio/manifest.json").read_text(encoding="utf-8"))
    durations = {entry["key"]: entry["seconds"] for entry in audio["scenes"]}

    (OUT_DIR / "shots").mkdir(parents=True, exist_ok=True)
    from playwright.sync_api import sync_playwright

    state: dict[str, Any] = {}
    if args.audit_id:
        state["last_audit"] = args.audit_id
    # Manifestul se completeaza, nu se rescrie: o reluare partiala trebuie sa
    # pastreze scenele deja filmate.
    entries = (
        json.loads(MANIFEST.read_text(encoding="utf-8"))["scenes"]
        if MANIFEST.exists()
        else []
    )
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch()
        for scene in STORYBOARD:
            if args.only and scene.key not in args.only:
                continue
            target = max(durations.get(scene.key, 6.0), scene.min_seconds)
            if scene.action == "upload":
                state["current"] = None
            print(f"  {scene.key:18s} ținta {target:5.1f}s ...", flush=True)
            began = time.time()
            shots = record_scene(browser, scene, target, state, args.dashboard)
            if scene.action == "upload":
                state["current"] = state.get(scene.sample)
            actual = time.time() - began
            entries = [item for item in entries if item["key"] != scene.key]
            entries.append(
                {
                    "key": scene.key,
                    "shots": str(shots.directory),
                    "frames": shots.count,
                    "target_seconds": round(target, 3),
                    "recorded_seconds": round(actual, 3),
                }
            )
            print(f"    {actual:6.1f}s real, {shots.count} cadre", flush=True)
            MANIFEST.write_text(
                json.dumps({"viewport": VIEWPORT, "scenes": entries}, indent=2),
                encoding="utf-8",
            )
        browser.close()

    MANIFEST.write_text(
        json.dumps({"viewport": VIEWPORT, "scenes": entries}, indent=2),
        encoding="utf-8",
    )
    print(f"\nscris {MANIFEST}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
