"""
router.py - High-speed instruction router dispatching to domain coprocessors.
"""
from typing import Dict, Optional, Type

from harness.coproc.base import BaseCoprocessor, CoprocessorContext
from harness.coproc.registry import CoprocessorRegistry
from harness.core.model import Instruction


class CoprocessorRouter:
    """
    Routes incoming decoded Instructions to the appropriate domain Coprocessor
    based on the routing table compiled by CoprocessorRegistry.
    """

    def __init__(self, registry: CoprocessorRegistry, ctx: CoprocessorContext):
        self.registry = registry
        self.ctx = ctx
        self._routes: Dict[Type[Instruction], BaseCoprocessor] = registry.build_handlers(ctx)

    def handle_instruction(self, inst: Instruction) -> None:
        """Route the instruction to its designated coprocessor handler."""
        inst_type = type(inst)
        handler = self._routes.get(inst_type)
        if handler is not None:
            handler.handle_instruction(inst)
        else:
            # Unrouted / unsupported instruction
            pass

    def get_coprocessor(self, cls: Type[BaseCoprocessor]) -> Optional[BaseCoprocessor]:
        """Helper to retrieve an instantiated coprocessor by its class."""
        for handler in self._routes.values():
            if isinstance(handler, cls):
                return handler
        return None
