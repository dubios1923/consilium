"""Incarcarea pragurilor din config.yaml, cu esec imediat la chei lipsa.

Un audit care cade inapoi pe o valoare implicita atunci cand configul e
incomplet raporteaza cifre pe care nimeni nu le-a ales. Aici o cheie lipsa este
o eroare, nu un default tacut.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml

DEFAULT_CONFIG_PATH = Path("config.yaml")


class ConfigError(RuntimeError):
    """Configuratie absenta, ilizibila sau incompleta."""


class Config:
    """Acces la praguri prin cai cu punct, fara valori implicite."""

    def __init__(self, data: dict[str, Any], source: Path | str = "<memorie>") -> None:
        if not isinstance(data, dict):
            raise ConfigError(f"{source}: radacina configului trebuie sa fie un dict")
        self._data = data
        self.source = str(source)

    @classmethod
    def load(cls, path: Path | str = DEFAULT_CONFIG_PATH) -> Config:
        config_path = Path(path)
        if not config_path.is_file():
            raise ConfigError(f"config inexistent: {config_path}")
        parsed = yaml.safe_load(config_path.read_text(encoding="utf-8"))
        return cls(parsed, source=config_path)

    def require(self, dotted_path: str) -> Any:
        """Intoarce valoarea de la calea data sau ridica ConfigError."""
        current: Any = self._data
        walked: list[str] = []
        for key in dotted_path.split("."):
            walked.append(key)
            if not isinstance(current, dict) or key not in current:
                raise ConfigError(
                    f"{self.source}: cheie lipsa `{'.'.join(walked)}` "
                    f"(ceruta ca `{dotted_path}`)"
                )
            current = current[key]
        if current is None:
            raise ConfigError(f"{self.source}: cheia `{dotted_path}` este goala")
        return current

    def require_float(self, dotted_path: str) -> float:
        value = self.require(dotted_path)
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise ConfigError(
                f"{self.source}: `{dotted_path}` trebuie sa fie numeric, "
                f"nu {type(value).__name__}"
            )
        return float(value)

    def require_int(self, dotted_path: str) -> int:
        value = self.require(dotted_path)
        if isinstance(value, bool) or not isinstance(value, int):
            raise ConfigError(
                f"{self.source}: `{dotted_path}` trebuie sa fie intreg, "
                f"nu {type(value).__name__}"
            )
        return value
