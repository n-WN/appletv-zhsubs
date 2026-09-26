"""Load the adjacent package when mitmdump runs this script."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from appletv_zhsubs.addon import addons  # noqa: F401
