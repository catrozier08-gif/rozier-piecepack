"""rozier-piecepack: a zero-swap layout fallback for circuits whose interaction graph is split into pieces.

Core search (rustworkx only):      piece_pack, whole_chip_vf2, fits, check_mapping
Qiskit integration (needs qiskit): transpile_with_fallback, PiecePackLayout
"""
from .core import BUDGET, FOUND, NONE, TIMEOUT, check_mapping, fits, piece_pack, whole_chip_vf2

__version__ = "0.3.0"
__all__ = ["piece_pack", "whole_chip_vf2", "fits", "check_mapping", "FOUND", "NONE", "TIMEOUT", "BUDGET",
           "transpile_with_fallback", "PiecePackLayout", "search_layout", "__version__"]


def __getattr__(name):   # import the Qiskit parts only when asked for, so the core works without Qiskit
    if name in ("transpile_with_fallback", "PiecePackLayout", "search_layout", "chip_graph", "interaction_graph"):
        from . import qiskit_tools
        return getattr(qiskit_tools, name)
    raise AttributeError(name)
