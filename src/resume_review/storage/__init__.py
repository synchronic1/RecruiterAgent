"""Filesystem primitives: path containment, atomic moves, topology."""

from __future__ import annotations

from .no_clobber import (
    FileIdentity,
    MoveOutcome,
    MoveResult,
    NoClobberUnsupported,
    atomic_no_clobber_move,
    ensure_directory,
    file_identity,
    no_clobber_backend_name,
    same_inode,
    same_volume,
    sha256_file,
)
from .paths import (
    PathEscape,
    SymlinkEscape,
    assert_no_reparse_traversal,
    assert_within_root,
    containment_report,
    extended_path,
    is_reparse_point,
    join_rel,
    normalize_rel_path,
    safe_final_component,
    to_absolute,
    to_relative,
)
from .topology import TopologyKind, TopologyReport, assert_supported_for_database, probe_topology

__all__ = [
    "FileIdentity",
    "MoveOutcome",
    "MoveResult",
    "NoClobberUnsupported",
    "PathEscape",
    "SymlinkEscape",
    "TopologyKind",
    "TopologyReport",
    "assert_no_reparse_traversal",
    "assert_supported_for_database",
    "assert_within_root",
    "atomic_no_clobber_move",
    "containment_report",
    "ensure_directory",
    "extended_path",
    "file_identity",
    "is_reparse_point",
    "join_rel",
    "no_clobber_backend_name",
    "normalize_rel_path",
    "probe_topology",
    "safe_final_component",
    "same_inode",
    "same_volume",
    "sha256_file",
    "to_absolute",
    "to_relative",
]
