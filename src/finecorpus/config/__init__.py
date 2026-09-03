"""Configuration package for Read The Fine Corpus.

Public API::

    from finecorpus.config import load_config, Config
    from finecorpus.config.preflight import run_preflight, PreflightReport

The authoritative configuration reference is
``docs/configuration/reference.md``; spec §4.6 mandates one declarative file
with nothing hidden in code.
"""

from .loader import load_config
from .models import Config
from .preflight import PreflightReport, run_preflight

__all__ = [
    "Config",
    "PreflightReport",
    "load_config",
    "run_preflight",
]
