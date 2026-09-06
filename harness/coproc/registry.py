"""
registry.py - Declarative coprocessor registry with class decorator.
"""
from typing import Dict, List, Type

from harness.coproc.base import BaseCoprocessor, CoprocessorContext
from harness.core.model import Instruction


class CoprocessorRegistry:
    def __init__(self):
        self._classes: List[Type[BaseCoprocessor]] = []

    def register(self, cls: Type[BaseCoprocessor]) -> Type[BaseCoprocessor]:
        """Class decorator to register a coprocessor class."""
        if not issubclass(cls, BaseCoprocessor):
            raise TypeError(f"{cls.__name__} must inherit from BaseCoprocessor")
        if cls not in self._classes:
            self._classes.append(cls)
        return cls

    def build_handlers(self, ctx: CoprocessorContext) -> Dict[Type[Instruction], BaseCoprocessor]:
        """
        Instantiates all registered coprocessors with the given context
        and returns a routing table mapping Instruction type -> coprocessor instance.
        """
        routes: Dict[Type[Instruction], BaseCoprocessor] = {}

        for cls in self._classes:
            instance = cls(ctx)
            for inst_type in instance.handled_instructions:
                if inst_type in routes:
                    existing = routes[inst_type].__class__.__name__
                    raise ValueError(
                        f"Routing conflict for instruction {inst_type.__name__}: "
                        f"already registered to {existing}, cannot also assign to {cls.__name__}"
                    )
                routes[inst_type] = instance

        return routes


# Global default registry instance
coprocessor_registry = CoprocessorRegistry()
register_coprocessor = coprocessor_registry.register
