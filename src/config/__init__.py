"""Split config package — re-exports everything from submodules.

Original src/config.py was 1018 lines; split by responsibility in Phase 0-1.
All names are re-exported so `from src import config; config.XXX` keeps working.
"""
from __future__ import annotations

from .core import *
from .llm import *
from .db import *
from .retrieve import *
from .audit import *
from .indicators import *
