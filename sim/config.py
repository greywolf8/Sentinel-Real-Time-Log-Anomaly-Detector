"""Shared configuration loading for the simulator (docs/sentinel-plan.md sections 4 and 6.1).

``sim/catalog.yaml`` holds the vocabulary (services, components, codes, templates) and
``sim/platform.yaml`` holds the tunable behaviour (rps, error rates, latency, dependencies,
propagation). Both are data files: the detector reads ``catalog.yaml`` directly and never
imports anything from this package.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

SIM_DIR = Path(__file__).resolve().parent
CATALOG_PATH = SIM_DIR / "catalog.yaml"
PLATFORM_PATH = SIM_DIR / "platform.yaml"

LEVEL_INFO = "I"
LEVEL_WARN = "W"
LEVEL_ERROR = "E"
LEVEL_FATAL = "F"
VALID_LEVELS: frozenset[str] = frozenset({LEVEL_INFO, LEVEL_WARN, LEVEL_ERROR, LEVEL_FATAL})
SUCCESS_CODE = "00000"
SERVICE_CODES: tuple[str, ...] = ("PAY", "CLM", "ELG", "PRV", "ADM")


class ConfigError(ValueError):
    """Raised when catalog.yaml or platform.yaml is missing or internally inconsistent."""


@dataclass(frozen=True, slots=True)
class CodeSpec:
    """One catalog entry: a component's error or warning code."""

    code: str
    service: str
    component: str
    level: str
    weight: float
    template: str
    latency_ms: float
    latency_min_ms: float
    denial: str | None = None


@dataclass(frozen=True, slots=True)
class ComponentSpec:
    """A component in the catalog: its service, name, criticality and success template."""

    code: str
    service: str
    name: str
    criticality: float
    success_template: str


@dataclass(frozen=True, slots=True)
class PropagationEdge:
    """One dependency edge from section 4.6: upstream deviation leaks downstream after a lag."""

    src: str
    dst: str
    coupling: float
    lag_s: float


@dataclass(frozen=True, slots=True)
class DailyBand:
    """A band of hours of day with a volume and error-rate multiplier."""

    hours: frozenset[int]
    volume: float
    err: float


@dataclass(slots=True)
class ComponentConfig:
    """Tunable behaviour of one component, from platform.yaml."""

    code: str
    service: str
    rps: float
    err: float
    p50_ms: float
    depends_on: tuple[str, ...]
    warn: float = 0.0
    denials: tuple[str, ...] = ()


@dataclass(slots=True)
class Catalog:
    """Parsed catalog.yaml."""

    services: dict[str, str] = field(default_factory=dict)
    components: dict[str, ComponentSpec] = field(default_factory=dict)
    codes: dict[str, CodeSpec] = field(default_factory=dict)
    # component code -> codes, split by level so the generator never scans the whole catalog
    error_codes: dict[str, list[CodeSpec]] = field(default_factory=dict)
    warn_codes: dict[str, list[CodeSpec]] = field(default_factory=dict)

    def key_of(self, component: str) -> str:
        """The ``SVC.CMP`` key a control API call uses, e.g. CLM.STR."""
        return f"{self.components[component].service}.{component}"


@dataclass(slots=True)
class Platform:
    """Parsed platform.yaml."""

    tick_ms: int
    start_epoch_ms: int
    jitter: float
    daily_enabled: bool
    daily: list[DailyBand]
    demo_start_hour_utc: int | None
    latency_sigma: float
    latency_min_ms: int
    latency_max_ms: int
    components: dict[str, ComponentConfig] = field(default_factory=dict)
    propagation: list[PropagationEdge] = field(default_factory=list)
    latency_propagation: dict[str, float] = field(default_factory=dict)
    denial_mix: dict[str, float] = field(default_factory=dict)

    def daily_multipliers(self, hour_utc: int) -> tuple[float, float]:
        for band in self.daily:
            if hour_utc in band.hours:
                return band.volume, band.err
        return 1.0, 1.0

    def downstream_of(self, component: str) -> tuple[str, ...]:
        """Components this one feeds, i.e. the cascade the incident engine will group."""
        return tuple(e.dst for e in self.propagation if e.src == component)

    def upstream_of(self, component: str) -> tuple[str, ...]:
        """Components this one depends on."""
        return tuple(e.src for e in self.propagation if e.dst == component)


def _require(mapping: dict[str, Any], key: str, where: str) -> Any:
    if key not in mapping:
        raise ConfigError(f"missing {key!r} in {where}")
    return mapping[key]


def load_catalog(path: Path = CATALOG_PATH) -> Catalog:
    """Read and validate catalog.yaml."""
    raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(raw, dict):
        raise ConfigError(f"{path} did not parse to a mapping")

    catalog = Catalog()
    for code, name in _require(raw, "services", str(path)).items():
        if len(code) != 3:
            raise ConfigError(f"service code {code!r} is not 3 characters")
        catalog.services[code] = str(name)

    for code, spec in _require(raw, "components", str(path)).items():
        if len(code) != 3:
            raise ConfigError(f"component code {code!r} is not 3 characters")
        service = str(_require(spec, "service", f"components.{code}"))
        if service not in catalog.services:
            raise ConfigError(f"components.{code}.service {service!r} is not a known service")
        catalog.components[code] = ComponentSpec(
            code=code,
            service=service,
            name=str(_require(spec, "name", f"components.{code}")),
            criticality=float(spec.get("criticality", 0.5)),
            success_template=str(_require(spec, "success", f"components.{code}")),
        )

    for code, spec in _require(raw, "codes", str(path)).items():
        if len(code) != 5:
            raise ConfigError(f"error code {code!r} is not 5 characters")
        service = str(_require(spec, "service", f"codes.{code}"))
        component = str(_require(spec, "component", f"codes.{code}"))
        if component not in catalog.components:
            raise ConfigError(f"codes.{code}.component {component!r} is unknown")
        if catalog.components[component].service != service:
            raise ConfigError(f"codes.{code} service {service} contradicts components.{component}")
        level = str(_require(spec, "level", f"codes.{code}"))
        if level not in (LEVEL_ERROR, LEVEL_WARN, LEVEL_FATAL):
            raise ConfigError(f"codes.{code}.level {level!r} is not E, W or F")
        entry = CodeSpec(
            code=code,
            service=service,
            component=component,
            level=level,
            weight=float(_require(spec, "weight", f"codes.{code}")),
            template=str(_require(spec, "template", f"codes.{code}")),
            latency_ms=float(spec.get("latency_ms", 25.0)),
            latency_min_ms=float(spec.get("latency_min_ms", 5.0)),
            denial=spec.get("denial"),
        )
        if entry.weight <= 0.0:
            raise ConfigError(f"codes.{code}.weight must be positive")
        catalog.codes[code] = entry
        bucket = catalog.error_codes if level in (LEVEL_ERROR, LEVEL_FATAL) else catalog.warn_codes
        bucket.setdefault(component, []).append(entry)

    return catalog


def load_platform(path: Path = PLATFORM_PATH, catalog: Catalog | None = None) -> Platform:
    """Read and validate platform.yaml."""
    raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(raw, dict):
        raise ConfigError(f"{path} did not parse to a mapping")
    plat_raw = _require(raw, "platform", str(path))
    cat = catalog or load_catalog()

    daily_raw = plat_raw.get("daily", {})
    daily_cfg = daily_raw if isinstance(daily_raw, dict) else {}
    daily: list[DailyBand] = []
    for band in daily_cfg.get("curve", []) or []:
        daily.append(
            DailyBand(
                hours=frozenset(int(h) for h in band.get("hours", [])),
                volume=float(band.get("volume", 1.0)),
                err=float(band.get("err", 1.0)),
            )
        )

    lat = _require(plat_raw, "latency", "platform")
    platform = Platform(
        tick_ms=int(plat_raw.get("tick_ms", 50)),
        start_epoch_ms=int(_require(plat_raw, "start_epoch_ms", "platform")),
        jitter=float(plat_raw.get("jitter", 0.0)),
        daily_enabled=bool(daily_cfg.get("enabled", False)),
        daily=daily,
        demo_start_hour_utc=(
            int(plat_raw["demo_start_hour_utc"]) if plat_raw.get("demo_start_hour_utc") is not None else None
        ),
        latency_sigma=float(_require(lat, "sigma", "platform.latency")),
        latency_min_ms=int(_require(lat, "min_ms", "platform.latency")),
        latency_max_ms=int(_require(lat, "max_ms", "platform.latency")),
        denial_mix={str(k): float(v) for k, v in (raw.get("denial_mix") or {}).items()},
        latency_propagation={
            str(k): float(v) for k, v in (raw.get("latency_propagation") or {}).items()
        },
    )

    for code, spec in _require(raw, "components", str(path)).items():
        if code not in cat.components:
            raise ConfigError(f"platform.components.{code} is not in the catalog")
        # depends_on entries are written as SVC.CMP in the plan's tables; they are stored
        # as bare component codes, which is what the propagation edges key on.
        depends: list[str] = []
        for raw_dep in spec.get("depends_on", []) or []:
            dep = str(raw_dep).split(".")[-1]
            if dep not in cat.components:
                raise ConfigError(f"platform.components.{code} depends on unknown {raw_dep}")
            if dep not in depends:
                depends.append(dep)
        platform.components[code] = ComponentConfig(
            code=code,
            service=cat.components[code].service,
            rps=float(_require(spec, "rps", f"platform.components.{code}")),
            err=float(spec.get("err", 0.0)),
            p50_ms=float(_require(spec, "p50_ms", f"platform.components.{code}")),
            depends_on=tuple(depends),
            warn=float(spec.get("warn", 0.0)),
            denials=tuple(str(d) for d in spec.get("denials", []) or []),
        )

    for index, edge in enumerate(raw.get("propagation", []) or []):
        src = str(_require(edge, "from", "propagation")).split(".")[-1]
        dst = str(_require(edge, "to", "propagation")).split(".")[-1]
        for endpoint in (src, dst):
            if endpoint not in platform.components:
                raise ConfigError(f"propagation[{index}] endpoint {endpoint} is not a component")
        platform.propagation.append(
            PropagationEdge(
                src=src,
                dst=dst,
                coupling=float(_require(edge, "coupling", f"propagation[{index}]")),
                lag_s=float(_require(edge, "lag_s", f"propagation[{index}]")),
            )
        )

    return platform


def load_all() -> tuple[Catalog, Platform]:
    """Load both config files, the common entry point for the simulator."""
    catalog = load_catalog()
    return catalog, load_platform(catalog=catalog)
