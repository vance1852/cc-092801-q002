"""靶点赛道竞争快照与差异化窗口比较服务。"""

from .analysis import ALGORITHM_VERSION, compare, derive_groups
from .service import TrackIntelService

__all__ = [
    "ALGORITHM_VERSION",
    "TrackIntelService",
    "compare",
    "derive_groups",
]

__version__ = "0.1.0"
