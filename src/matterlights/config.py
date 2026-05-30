from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
import os


@dataclass(slots=True)
class Settings:
    ha_url: str
    ha_token: str
    light_entities: list[str]
    light_zone_layout: list[str] = field(default_factory=list)
    light_zone_file: Path | None = None
    preview_override_file: Path | None = None
    color_sync_mode: str = "zoned"
    primary_light_zone_names: list[str] = field(default_factory=list)
    zone_ui_port: int = 8765
    dashboard_port: int = 8770
    screen_capture_target: str = "primary"
    request_timeout_seconds: float = 3.0
    availability_refresh_seconds: float = 5.0
    sync_interval_seconds: float = 0.2
    inter_light_delay_seconds: float = 0.4
    max_parallel_light_updates: int = 1
    color_change_threshold: int = 12
    brightness_floor: int = 0
    transition_seconds: float = 0.0
    dark_threshold: int = 12
    dark_active_ratio_threshold: float = 0.05
    color_boost: float = 1.15
    sample_stride: int = 24
    error_retry_seconds: float = 5.0
    log_path: Path | None = None


def load_settings(*, require_light_entities: bool = True) -> Settings:
    dotenv_path = _resolve_dotenv_path()
    env_values = _read_dotenv(dotenv_path)
    path_base_dir = _settings_base_dir(dotenv_path)

    def get_value(name: str, default: str | None = None) -> str:
        value = os.getenv(name)
        if value is not None and value != "":
            return value
        if name in env_values and env_values[name] != "":
            return env_values[name]
        if default is not None:
            return default
        raise ValueError(f"Missing required setting: {name}")

    entity_text = get_value("HA_LIGHT_ENTITIES", "")
    light_entities = [item.strip() for item in entity_text.split(",") if item.strip()]
    if require_light_entities and not light_entities:
        raise ValueError("HA_LIGHT_ENTITIES must contain at least one light entity ID")

    settings = Settings(
        ha_url=get_value("HA_URL", "http://192.168.1.2:8123").rstrip("/"),
        ha_token=get_value("HA_TOKEN"),
        light_entities=light_entities,
        light_zone_layout=_parse_light_zone_layout(get_value("LIGHT_ZONE_LAYOUT", "")),
        light_zone_file=_parse_optional_path(
            get_value("LIGHT_ZONE_FILE", str(_default_zone_file_path(path_base_dir))),
            path_base_dir,
        ),
        preview_override_file=_parse_optional_path(
            get_value("PREVIEW_OVERRIDE_FILE", str(_default_preview_override_file_path(path_base_dir))),
            path_base_dir,
        ),
        color_sync_mode=get_value("COLOR_SYNC_MODE", "zoned").strip().lower(),
        primary_light_zone_names=_parse_light_zone_layout(
            get_value("PRIMARY_LIGHT_ZONE_NAMES", "top-center,bottom-left")
        ),
        zone_ui_port=int(get_value("ZONE_UI_PORT", "8765")),
        dashboard_port=int(get_value("DASHBOARD_PORT", "8770")),
        screen_capture_target=get_value("SCREEN_CAPTURE_TARGET", "primary"),
        request_timeout_seconds=float(get_value("REQUEST_TIMEOUT_SECONDS", "3.0")),
        availability_refresh_seconds=float(get_value("AVAILABILITY_REFRESH_SECONDS", "5.0")),
        sync_interval_seconds=float(get_value("SYNC_INTERVAL_SECONDS", "0.2")),
        inter_light_delay_seconds=float(get_value("INTER_LIGHT_DELAY_SECONDS", "0.4")),
        max_parallel_light_updates=int(get_value("MAX_PARALLEL_LIGHT_UPDATES", "1")),
        color_change_threshold=int(get_value("COLOR_CHANGE_THRESHOLD", "12")),
        brightness_floor=int(get_value("BRIGHTNESS_FLOOR", "0")),
        transition_seconds=float(get_value("TRANSITION_SECONDS", "0.0")),
        dark_threshold=int(get_value("DARK_THRESHOLD", "12")),
        dark_active_ratio_threshold=float(get_value("DARK_ACTIVE_RATIO_THRESHOLD", "0.05")),
        color_boost=float(get_value("COLOR_BOOST", "1.15")),
        sample_stride=int(get_value("SAMPLE_STRIDE", "24")),
        error_retry_seconds=float(get_value("ERROR_RETRY_SECONDS", "5.0")),
        log_path=_parse_log_path(get_value("LOG_PATH", str(_default_log_path(path_base_dir))), path_base_dir),
    )
    _validate_settings(settings)
    return settings


def _read_dotenv(path: Path) -> dict[str, str]:
    if not path.exists():
        return {}

    values: dict[str, str] = {}
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        values[key.strip()] = value.strip()
    return values


def _resolve_dotenv_path() -> Path:
    explicit_path = os.getenv("MATTERLIGHTS_ENV_FILE")
    if explicit_path:
        return Path(explicit_path).expanduser()

    cwd_path = Path.cwd() / ".env"
    if cwd_path.exists():
        return cwd_path

    project_root_path = Path(__file__).resolve().parents[2] / ".env"
    if project_root_path.exists():
        return project_root_path

    return cwd_path


def _default_log_path(base_dir: Path | None = None) -> Path:
    local_app_data = os.getenv("LOCALAPPDATA")
    if local_app_data:
        return Path(local_app_data) / "matterlights" / "matterlights.log"
    return (base_dir or Path.cwd()) / "matterlights.log"


def _default_zone_file_path(base_dir: Path | None = None) -> Path:
    return (base_dir or Path.cwd()) / ".matterlights-zones.json"


def _default_preview_override_file_path(base_dir: Path | None = None) -> Path:
    return (base_dir or Path.cwd()) / ".matterlights-preview.json"


def _parse_log_path(value: str, base_dir: Path) -> Path | None:
    if not value.strip():
        return None
    return _resolve_path(Path(value).expanduser(), base_dir)


def _parse_optional_path(value: str, base_dir: Path) -> Path | None:
    if not value.strip():
        return None
    return _resolve_path(Path(value).expanduser(), base_dir)


def _parse_light_zone_layout(value: str) -> list[str]:
    if not value.strip():
        return []
    return [item.strip() for item in value.split(",") if item.strip()]


def _settings_base_dir(dotenv_path: Path) -> Path:
    if dotenv_path.exists():
        return dotenv_path.parent
    return Path(__file__).resolve().parents[2]


def _resolve_path(path: Path, base_dir: Path) -> Path:
    if path.is_absolute():
        return path
    return base_dir / path


def _validate_settings(settings: Settings) -> None:
    if settings.light_zone_layout and len(settings.light_zone_layout) != len(settings.light_entities):
        raise ValueError("LIGHT_ZONE_LAYOUT must contain one zone name per configured light entity")
    if settings.color_sync_mode not in {"zoned", "shared-variant"}:
        raise ValueError("COLOR_SYNC_MODE must be 'zoned' or 'shared-variant'")
    if settings.color_sync_mode == "shared-variant" and not settings.primary_light_zone_names:
        raise ValueError("PRIMARY_LIGHT_ZONE_NAMES must contain at least one zone name when COLOR_SYNC_MODE=shared-variant")
    capture_target = settings.screen_capture_target.strip().lower()
    if capture_target != "primary" and capture_target != "all":
        try:
            if int(capture_target) < 1:
                raise ValueError
        except ValueError as exc:
            raise ValueError("SCREEN_CAPTURE_TARGET must be 'primary', 'all', or a monitor index starting at 1") from exc
    if not 1 <= settings.zone_ui_port <= 65535:
        raise ValueError("ZONE_UI_PORT must be between 1 and 65535")
    if not 1 <= settings.dashboard_port <= 65535:
        raise ValueError("DASHBOARD_PORT must be between 1 and 65535")
    if settings.request_timeout_seconds <= 0:
        raise ValueError("REQUEST_TIMEOUT_SECONDS must be greater than 0")
    if settings.availability_refresh_seconds <= 0:
        raise ValueError("AVAILABILITY_REFRESH_SECONDS must be greater than 0")
    if settings.sync_interval_seconds <= 0:
        raise ValueError("SYNC_INTERVAL_SECONDS must be greater than 0")
    if settings.inter_light_delay_seconds < 0:
        raise ValueError("INTER_LIGHT_DELAY_SECONDS cannot be negative")
    if settings.max_parallel_light_updates < 1:
        raise ValueError("MAX_PARALLEL_LIGHT_UPDATES must be at least 1")
    if settings.color_change_threshold < 0:
        raise ValueError("COLOR_CHANGE_THRESHOLD cannot be negative")
    if not 0 <= settings.brightness_floor <= 255:
        raise ValueError("BRIGHTNESS_FLOOR must be between 0 and 255")
    if not 0 <= settings.dark_threshold <= 255:
        raise ValueError("DARK_THRESHOLD must be between 0 and 255")
    if not 0 <= settings.dark_active_ratio_threshold <= 1:
        raise ValueError("DARK_ACTIVE_RATIO_THRESHOLD must be between 0 and 1")
    if settings.color_boost <= 0:
        raise ValueError("COLOR_BOOST must be greater than 0")
    if settings.transition_seconds < 0:
        raise ValueError("TRANSITION_SECONDS cannot be negative")
    if settings.sample_stride <= 0:
        raise ValueError("SAMPLE_STRIDE must be greater than 0")
    if settings.error_retry_seconds <= 0:
        raise ValueError("ERROR_RETRY_SECONDS must be greater than 0")
