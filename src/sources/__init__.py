"""Source registry."""
from __future__ import annotations

from pathlib import Path
from typing import Optional

from src.sources.base import JobSource
from src.sources.himalayas import HimalayasSource
from src.sources.remoteok import RemoteOKSource
from src.sources.remotive import RemotiveSource
from src.sources.weworkremotely import WeWorkRemotelySource

REGISTRY: dict[str, type[JobSource]] = {
    "himalayas": HimalayasSource,
    "remotive": RemotiveSource,
    "weworkremotely": WeWorkRemotelySource,
    "remoteok": RemoteOKSource,
}


def build_sources(cfg: dict, cache_dir: Optional[Path] = None) -> list[JobSource]:
    """Instantiate every source enabled in config.yaml (sources.<name>.enabled)."""
    sources: list[JobSource] = []
    for name, options in (cfg.get("sources") or {}).items():
        if not (options or {}).get("enabled", False):
            continue
        cls = REGISTRY.get(name)
        if cls is None:
            raise ValueError(f"Unknown source in config: {name!r}")
        sources.append(cls(cfg["http"], cache_dir=cache_dir, options=options))
    return sources
