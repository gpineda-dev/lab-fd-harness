"""
bus.py - Inter-Process Bus Coprocessor.
Allows child scripts to subscribe to and publish on an in-memory event bus.
"""
from typing import ClassVar, Tuple, Type

from harness.coproc.base import BaseCoprocessor, CoprocessorContext
from harness.coproc.registry import register_coprocessor
from harness.core.model import BusEmit, BusEvent, BusSubscribe, Instruction


@register_coprocessor
class BusCoprocessor(BaseCoprocessor):
    handled_instructions: ClassVar[Tuple[Type[Instruction], ...]] = (
        BusSubscribe,
        BusEmit,
    )

    def __init__(self, ctx: CoprocessorContext):
        super().__init__(ctx)
        self.emit_event = ctx.emit_event
        self.bus = ctx.bus

    def handle_instruction(self, inst: Instruction) -> None:
        if isinstance(inst, BusSubscribe):
            self._handle_subscribe(inst)
        elif isinstance(inst, BusEmit):
            self._handle_emit(inst)

    def _handle_subscribe(self, inst: BusSubscribe) -> None:
        if self.bus is None:
            return

        def on_bus_event(topic: str, payload: str):
            self.emit_event(BusEvent(topic=topic, payload=payload))

        self.bus.subscribe(inst.topic, on_bus_event)

    def _handle_emit(self, inst: BusEmit) -> None:
        if self.bus is None:
            return
        self.bus.emit(inst.topic, inst.payload)
