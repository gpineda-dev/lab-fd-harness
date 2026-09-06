"""
sprint.py - String Interpolation and Bracket Templating Coprocessor.
Evaluates embedded expressions and reads variables from shared context.
"""
from typing import ClassVar, Tuple, Type

from harness.coproc.base import BaseCoprocessor, CoprocessorContext
from harness.coproc.registry import register_coprocessor
from harness.core.model import Instruction, SprintRequest, SprintResult
from harness.utils.pratt import interpolate_template


@register_coprocessor
class SprintCoprocessor(BaseCoprocessor):
    handled_instructions: ClassVar[Tuple[Type[Instruction], ...]] = (SprintRequest,)

    def __init__(self, ctx: CoprocessorContext):
        super().__init__(ctx)
        self.emit_event = ctx.emit_event

    def handle_instruction(self, inst: Instruction) -> None:
        if isinstance(inst, SprintRequest):
            self._handle_sprint(inst)

    def _handle_sprint(self, inst: SprintRequest):
        try:
            rendered = interpolate_template(inst.template, variables=self.ctx.variables)
        except Exception as e:
            rendered = f"error:{e}"
        self.emit_event(SprintResult(text=rendered))
