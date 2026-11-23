"""Offline on-call privileged approval tool.

The package deliberately uses only the Python standard library and SQLite.
"""

from .clock import ManualClock, SystemClock
from .models import Result
from .service import ApprovalService

__all__ = ["ApprovalService", "ManualClock", "Result", "SystemClock"]

__version__ = "1.0.0"
