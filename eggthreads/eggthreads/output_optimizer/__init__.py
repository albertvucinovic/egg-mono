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
from .classify import (
    PathLineContent,
    is_plausible_path_list_line,
    normalize_command_name,
    parse_path_list_lines,
    parse_path_line_content,
    parse_path_line_content_lines,
    request_command_name,
    simple_bash_command_invocation,
    simple_bash_command_name,
    simple_bash_command_words,
)
from .factory import create_default_output_optimizer, default_native_filters
from .filters.find import FindPathGroupFilter, is_find_like_request, parse_find_fd_paths
from .filters.git_status import GitStatusCompactFilter, is_git_status_request, parse_git_status_entries, parse_git_status_line
from .filters.grep import GrepRgGroupByFileFilter, is_grep_like_request, parse_grep_rg_matches
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
    "FindPathGroupFilter",
    "GenericOutputFilter",
    "GitStatusCompactFilter",
    "GrepRgGroupByFileFilter",
    "OptimizeDecision",
    "OptimizeRequest",
    "OutputFilter",
    "OutputOptimizer",
    "OutputOptimizerRegistry",
    "PathLineContent",
    "ProgressNoiseFilter",
    "RepeatedLineDedupeFilter",
    "bounded_head_tail",
    "calculate_size_metadata",
    "clean_ansi_controls",
    "create_default_output_optimizer",
    "create_generic_output_optimizer",
    "dedupe_repeated_lines",
    "default_native_filters",
    "default_generic_filters",
    "is_find_like_request",
    "is_git_status_request",
    "is_grep_like_request",
    "is_obvious_progress_noise_line",
    "is_plausible_path_list_line",
    "is_truthy_output_optimizer_flag",
    "make_decision",
    "normalize_command_name",
    "output_optimizer_enabled",
    "parse_find_fd_paths",
    "parse_git_status_entries",
    "parse_git_status_line",
    "parse_grep_rg_matches",
    "parse_path_list_lines",
    "parse_path_line_content",
    "parse_path_line_content_lines",
    "request_command_name",
    "simple_bash_command_invocation",
    "simple_bash_command_name",
    "simple_bash_command_words",
    "suppress_progress_noise",
]
