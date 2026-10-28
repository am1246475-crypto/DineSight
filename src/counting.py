"""
Compatibility wrapper pointing to src/count.py
"""
import sys
from pathlib import Path

# Add src to path if needed
sys.path.insert(0, str(Path(__file__).parent))

from count import (
    LineCrossingCounter,
    TableOccupancyTracker,
    DineSightCounter,
    signed_side,
    distance_to_segment,
)

__all__ = [
    "LineCrossingCounter",
    "TableOccupancyTracker",
    "DineSightCounter",
    "signed_side",
    "distance_to_segment",
]

