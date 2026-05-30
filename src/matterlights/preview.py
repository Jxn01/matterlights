from __future__ import annotations

from dataclasses import dataclass
import json
from pathlib import Path
import time

from matterlights.screen import RgbColor


@dataclass(frozen=True, slots=True)
class PreviewOverride:
    entity_id: str
    color: RgbColor
    brightness: int
    expires_at: float


def load_preview_overrides(path: Path | None) -> dict[str, PreviewOverride]:
    if path is None or not path.exists():
        return {}

    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}

    now = time.time()
    active_overrides: dict[str, PreviewOverride] = {}
    expired_found = False
    for entry in payload.get("overrides", []):
        expires_at = float(entry.get("expires_at", 0))
        if expires_at <= now:
            expired_found = True
            continue

        override = PreviewOverride(
            entity_id=str(entry["entity_id"]),
            color=RgbColor(*[int(channel) for channel in entry["color"]]),
            brightness=int(entry["brightness"]),
            expires_at=expires_at,
        )
        active_overrides[override.entity_id] = override

    if expired_found:
        _write_preview_overrides(path, active_overrides)

    return active_overrides


def activate_preview_override(
    path: Path | None,
    entity_id: str,
    color: RgbColor,
    brightness: int,
    duration_seconds: float,
) -> PreviewOverride | None:
    if path is None:
        return None

    active_overrides = load_preview_overrides(path)
    override = PreviewOverride(
        entity_id=entity_id,
        color=color,
        brightness=brightness,
        expires_at=time.time() + duration_seconds,
    )
    active_overrides[entity_id] = override
    _write_preview_overrides(path, active_overrides)
    return override


def _write_preview_overrides(path: Path, overrides: dict[str, PreviewOverride]) -> None:
    payload = {
        "version": 1,
        "overrides": [
            {
                "entity_id": override.entity_id,
                "color": override.color.as_list(),
                "brightness": override.brightness,
                "expires_at": override.expires_at,
            }
            for override in overrides.values()
        ],
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2), encoding="utf-8")