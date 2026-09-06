"""
calc.py - Scientific Pratt Calculator and Variables Coprocessor.
Evaluates expressions, executes scientific functions, and stores variables in memory.
"""
from typing import ClassVar, Tuple, Type

from harness.coproc.base import BaseCoprocessor, CoprocessorContext
from harness.coproc.registry import register_coprocessor
from harness.core.model import CalcListRequest, CalcListResult, CalcRequest, CalcResult, Instruction
from harness.utils.pratt import evaluate_expression


@register_coprocessor
class CalcCoprocessor(BaseCoprocessor):
    handled_instructions: ClassVar[Tuple[Type[Instruction], ...]] = (CalcRequest, CalcListRequest)

    def __init__(self, ctx: CoprocessorContext):
        super().__init__(ctx)
        self.emit_event = ctx.emit_event

    def handle_instruction(self, inst: Instruction) -> None:
        if isinstance(inst, CalcRequest):
            self._handle_calc(inst)
        elif isinstance(inst, CalcListRequest):
            self._handle_list(inst)

    def _handle_calc(self, inst: CalcRequest):
        try:
            val = evaluate_expression(inst.expr, variables=self.ctx.variables)
            if val.is_integer():
                formatted = str(int(val))
            else:
                formatted = f"{val:.4f}".rstrip("0").rstrip(".")
            if inst.store:
                self.ctx.variables[inst.store] = val
                formatted = f"{inst.store}={formatted}"
        except Exception as e:
            formatted = f"error:{e}"
        self.emit_event(CalcResult(value=formatted))

    def _handle_list(self, _inst: CalcListRequest):
        if self.ctx.variables:
            items = []
            for k, v in sorted(self.ctx.variables.items()):
                if isinstance(v, (int, float)) and hasattr(v, "is_integer") and v.is_integer():
                    val_str = str(int(v))
                else:
                    val_str = f"{v:.4f}".rstrip("0").rstrip(".")
                items.append(f"{k}={val_str}")
            vars_str = ",".join(items)
        else:
            vars_str = "none"

        self.emit_event(CalcListResult(variables=vars_str, count=len(self.ctx.variables)))
