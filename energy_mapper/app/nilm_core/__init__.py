"""nilm_core: non-intrusive load monitoring from a whole-house power meter.

Pure Python (standard library only), independent from Home Assistant so it can
be tested and replayed offline. Step pairing and the cycling-session idea were
informed by Home NILM (https://github.com/amstrad83/ha-home-nilm, MIT).
"""

from .activations import Activation, find_cycling_sessions, pair_steps
from .known import KnownLoad, SwitchedLoadConfig, resolve_switched_load
from .live import LiveDetector, replay
from .report import AnalysisConfig, SubmeterConfig, analyze
from .signal import SampleBuilder, build_samples, parse_numeric
from .signatures import Signature, cluster
from .steps import Step, StepStream, detect_steps

__all__ = [
    "Activation",
    "AnalysisConfig",
    "KnownLoad",
    "LiveDetector",
    "SampleBuilder",
    "Signature",
    "Step",
    "StepStream",
    "SubmeterConfig",
    "SwitchedLoadConfig",
    "analyze",
    "build_samples",
    "cluster",
    "detect_steps",
    "find_cycling_sessions",
    "pair_steps",
    "parse_numeric",
    "replay",
    "resolve_switched_load",
]
