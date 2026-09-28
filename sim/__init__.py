"""Sentinel simulator: the demo Medicaid claims platform.

Package modules:
    config       loads catalog.yaml and platform.yaml (sections 4, 6.1)
    line_format  the 46-byte fixed-width header contract (section 5)
    clock        virtual clock for --speed (section 6.1)
    generator    20 component tasks, one writer, faults applied to draws
    faults       rate, mix, silence and volume overrides plus ground truth (section 6.2)
    control_api  localhost FastAPI the Fault Console talks to (section 6.2)
"""

from __future__ import annotations

__all__ = ["clock", "config", "control_api", "faults", "generator", "line_format"]
