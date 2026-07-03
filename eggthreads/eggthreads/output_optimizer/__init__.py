from __future__ import annotations

"""Pure Egg-native output optimizer package."""

from .core import (
    DEFAULT_OPTIMIZER_NAME,
    OptimizeDecision,
    OptimizeRequest,
    OutputFilter,
    OutputOptimizer,
    OutputOptimizerRegistry,
    calculate_size_metadata,
    make_decision,
)
from .config import OUTPUT_OPTIMIZER_ENV, is_truthy_output_optimizer_flag, output_optimizer_enabled
from .generic import (
    AnsiControlCleanupFilter,
    BoundedHeadTailFilter,
    GenericOutputFilter,
    ProgressNoiseFilter,
    RepeatedLineDedupeFilter,
    bounded_head_tail,
    clean_ansi_controls,
    create_generic_output_optimizer,
    dedupe_repeated_lines,
    default_generic_filters,
    is_obvious_progress_noise_line,
    suppress_progress_noise,
)

__all__ = [
    "DEFAULT_OPTIMIZER_NAME",
    "OUTPUT_OPTIMIZER_ENV",
    "AnsiControlCleanupFilter",
    "BoundedHeadTailFilter",
    "GenericOutputFilter",
    "OptimizeDecision",
    "OptimizeRequest",
    "OutputFilter",
    "OutputOptimizer",
    "OutputOptimizerRegistry",
    "ProgressNoiseFilter",
    "RepeatedLineDedupeFilter",
    "bounded_head_tail",
    "calculate_size_metadata",
    "clean_ansi_controls",
    "create_generic_output_optimizer",
    "dedupe_repeated_lines",
    "default_generic_filters",
    "is_obvious_progress_noise_line",
    "is_truthy_output_optimizer_flag",
    "make_decision",
    "output_optimizer_enabled",
    "suppress_progress_noise",
]
