"""Static backend catalog loading and repo-level backend configuration."""

from __future__ import annotations

import tomllib
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, Field, ValidationError

from orch.config import Config


class BackendCatalogError(ValueError):
    """Raised when backend catalog or override data is invalid."""

    @classmethod
    def missing_catalog(cls, path: Path) -> BackendCatalogError:
        """Build an error for a missing backend catalog path."""
        return cls(f"Backend catalog not found: {path}")


class BackendQuotaConfig(BaseModel):
    mode: Literal["unlimited", "fixed-window", "dynamic-429"]
    step_limit: int | None = Field(default=None, ge=0)
    dispatch_limit: int | None = Field(default=None, ge=0)
    window_seconds: int | None = Field(default=86_400, ge=1)


class BackendDefinition(BaseModel):
    id: str
    logical_agents: list[str] = Field(min_length=1)
    physical_alias: str
    model: str
    priority: int = 100
    concurrency: int = Field(default=1, ge=1)
    enabled: bool = True
    min_reserve: int | None = Field(default=None, ge=0)
    quota: BackendQuotaConfig


class BackendCatalog(BaseModel):
    backends: list[BackendDefinition] = Field(min_length=1)


def load_configured_backends(
    repo_root: Path, *, config: Config | None = None
) -> list[BackendDefinition]:
    """Load configured backends with repo-level overrides applied."""
    cfg = config or Config.load(repo_root=repo_root)
    backend_cfg = cfg.backends

    if not backend_cfg.enabled or not backend_cfg.catalog_paths:
        return []

    backends: list[BackendDefinition] = []
    seen_ids: dict[str, Path] = {}
    for catalog_path in backend_cfg.catalog_paths:
        path = Path(catalog_path)
        if not path.is_absolute():
            path = repo_root / path
        loaded = _load_catalog(path.resolve())
        for backend in loaded:
            if backend.id in seen_ids:
                first_path = seen_ids[backend.id]
                msg = (
                    f"Duplicate backend id '{backend.id}' in {path} "
                    f"(already defined in {first_path})."
                )
                raise BackendCatalogError(msg)
            seen_ids[backend.id] = path
            backends.append(_apply_override(backend, cfg))

    order_index = {backend_id: idx for idx, backend_id in enumerate(backend_cfg.order)}
    default_order = len(order_index)
    backends.sort(
        key=lambda backend: (
            backend.priority,
            order_index.get(backend.id, default_order),
            backend.id,
        )
    )
    return backends


def _load_catalog(path: Path) -> list[BackendDefinition]:
    if not path.is_file():
        raise BackendCatalogError.missing_catalog(path)

    with path.open("rb") as handle:
        raw = tomllib.load(handle)

    try:
        catalog = BackendCatalog.model_validate(raw)
    except ValidationError as exc:
        raise BackendCatalogError(_format_validation_error(path, exc)) from exc

    return catalog.backends


def _apply_override(backend: BackendDefinition, config: Config) -> BackendDefinition:
    override = config.backends.overrides.get(backend.id)
    if override is None:
        return backend

    updates = {
        field: value
        for field in ("enabled", "priority", "concurrency", "min_reserve")
        if (value := getattr(override, field)) is not None
    }
    return backend.model_copy(update=updates)


def _format_validation_error(path: Path, exc: ValidationError) -> str:
    parts = [f"Invalid backend catalog: {path}"]
    for error in exc.errors():
        location = ".".join(str(part) for part in error["loc"])
        parts.append(f"  {location}: {error['msg']}")
    return "\n".join(parts)


def fixed_window_start(quota: BackendQuotaConfig, timestamp: str) -> str | None:
    """Return the inclusive quota-window start for a fixed-window backend."""
    if quota.mode != "fixed-window" or quota.window_seconds is None:
        return None
    allocated_at = datetime.fromisoformat(timestamp)
    if allocated_at.tzinfo is None:
        allocated_at = allocated_at.replace(tzinfo=UTC)
    return (allocated_at - timedelta(seconds=quota.window_seconds)).isoformat()
