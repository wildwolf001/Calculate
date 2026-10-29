from __future__ import annotations

from typing import Any, Protocol


class ArtifactWriter(Protocol):
    def write(self, state: Any) -> dict[str, str]: ...
