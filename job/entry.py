"""Dispecer: aceeași imagine servește și launcher-ul, și job-ul.

`CONSILIUM_ROLE=launcher` pornește serviciul care primește evenimentele,
`dashboard` pagina de inspecție, orice altceva rulează pipeline-ul pentru un
singur obiect și iese.
"""

from __future__ import annotations

import os
import sys


def main() -> int:
    role = os.environ.get("CONSILIUM_ROLE", "job")
    if role in ("launcher", "dashboard"):
        import uvicorn

        target = (
            "job.launcher:app" if role == "launcher" else "consilium.dashboard:app"
        )
        uvicorn.run(
            target,
            host="0.0.0.0",
            port=int(os.environ.get("PORT", "8080")),
            log_level="info",
        )
        return 0

    from job.main import main as run_job

    return run_job()


if __name__ == "__main__":
    sys.exit(main())
