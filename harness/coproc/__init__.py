"""
coproc package - Modular domain coprocessors for fd-harness.
"""
from harness.coproc.base import BaseCoprocessor, CoprocessorContext
from harness.coproc.registry import CoprocessorRegistry, coprocessor_registry, register_coprocessor

# Import modules to trigger decorator auto-registration
from harness.coproc import timer, calc, sprint, bus, dlp
from harness.coproc.dlp import DlpCoprocessor
from harness.coproc.timer import ClockState, TimerCoprocessor

__all__ = [
    "BaseCoprocessor",
    "CoprocessorContext",
    "CoprocessorRegistry",
    "coprocessor_registry",
    "register_coprocessor",
    "ClockState",
    "TimerCoprocessor",
    "DlpCoprocessor",
]
