"""Six targeting strategies, one per player."""

from .bully_hunter import BullyHunter
from .equalizer import Equalizer
from .giant_slayer import GiantSlayer
from .terminator import Terminator
from .vindicator import Vindicator
from .wildcard import Wildcard

__all__ = ["Vindicator", "BullyHunter", "GiantSlayer", "Equalizer", "Wildcard", "Terminator"]
