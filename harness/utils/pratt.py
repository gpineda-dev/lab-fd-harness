"""
pratt.py - Modular Top-Down Operator Precedence (Pratt) Parser Architecture.
Decouples the immutable parsing engine from domain-specific grammars:
  1. TemporalGrammar: Strict temporal units & fractions (e.g. '1s / 60', '100ms * 2 + 50ms')
  2. MathGrammar: Scientific math, functions (sin, cos, sqrt, exp), constants, and variables
"""
from abc import ABC, abstractmethod
from dataclasses import dataclass
from enum import Enum, auto
import math
import re
from typing import Any, Callable, Dict, List, Optional, Union


# ==============================================================================
# Token Definitions
# ==============================================================================
class TokenType(Enum):
    NUMBER = auto()
    UNIT = auto()
    IDENT = auto()
    PLUS = auto()
    MINUS = auto()
    STAR = auto()
    SLASH = auto()
    PERCENT = auto()
    POW = auto()
    LPAREN = auto()
    RPAREN = auto()
    COMMA = auto()
    EOF = auto()


@dataclass
class Token:
    type: TokenType
    value: Union[float, str]


# ==============================================================================
# Universal Pratt Parsing Engine (Kernel)
# ==============================================================================
class PrattEngine:
    """
    Domain-agnostic Pratt Parser core.
    Operates strictly via Grammar callbacks for token binding and evaluations.
    """

    def __init__(self, tokens: List[Token], grammar: "Grammar"):
        self.tokens = tokens
        self.grammar = grammar
        self.pos = 0

    def current(self) -> Token:
        return self.tokens[self.pos]

    def advance(self) -> Token:
        tok = self.current()
        if tok.type != TokenType.EOF:
            self.pos += 1
        return tok

    def parse(self) -> Any:
        val = self.parse_expression(0)
        if self.current().type != TokenType.EOF:
            raise ValueError(f"Unexpected trailing token: {self.current()}")
        return val

    def parse_expression(self, rbp: int) -> Any:
        """Core Pratt loop: parses null denotation (nud) followed by left denotations (led)."""
        tok = self.advance()
        left = self.grammar.nud(self, tok)

        while self.grammar.lbp(self.current()) > rbp:
            op_tok = self.advance()
            left = self.grammar.led(self, op_tok, left)

        return left


# ==============================================================================
# Abstract Grammar Interface
# ==============================================================================
class Grammar(ABC):
    @abstractmethod
    def tokenize(self, text: str) -> List[Token]:
        """Lexes input string into domain-specific tokens."""
        pass

    @abstractmethod
    def lbp(self, token: Token) -> int:
        """Returns the Left Binding Power (precedence) for an operator."""
        pass

    @abstractmethod
    def nud(self, engine: PrattEngine, token: Token) -> Any:
        """Null Denotation: evaluates literals, prefixes, and grouped expressions."""
        pass

    @abstractmethod
    def led(self, engine: PrattEngine, token: Token, left: Any) -> Any:
        """Left Denotation: evaluates infix operators and postfix attributes."""
        pass


# ==============================================================================
# 1. Temporal Grammar (Dedicated to # @harness timing and durations)
# ==============================================================================
TEMPORAL_PRECEDENCE = {
    TokenType.PLUS: 10,
    TokenType.MINUS: 10,
    TokenType.STAR: 20,
    TokenType.SLASH: 20,
    TokenType.PERCENT: 20,
    TokenType.POW: 30,
    TokenType.UNIT: 40,  # Postfix unit multiplier binds tightest
}

TEMPORAL_UNITS = {
    "us": 0.000001,
    "µs": 0.000001,
    "ms": 0.001,
    "s": 1.0,
    "sec": 1.0,
    "m": 60.0,
    "min": 60.0,
    "h": 3600.0,
}


class TemporalGrammar(Grammar):
    """
    Strict grammar for cadence calculations and duration parsing.
    Prevents token collisions with variables and restricts arbitrary execution.
    """

    def tokenize(self, text: str) -> List[Token]:
        tokens: List[Token] = []
        pattern = re.compile(r"(\d+(?:\.\d+)?|\.\d+)|(us|µs|ms|sec|min|s|m|h)\b|(\*\*|\^)|([+\-*/%()])")
        for match in pattern.finditer(text):
            num, unit, pow_op, op = match.groups()
            if num is not None:
                tokens.append(Token(TokenType.NUMBER, float(num)))
            elif unit is not None:
                tokens.append(Token(TokenType.UNIT, unit))
            elif pow_op is not None:
                tokens.append(Token(TokenType.POW, "**"))
            elif op == "+":
                tokens.append(Token(TokenType.PLUS, "+"))
            elif op == "-":
                tokens.append(Token(TokenType.MINUS, "-"))
            elif op == "*":
                tokens.append(Token(TokenType.STAR, "*"))
            elif op == "/":
                tokens.append(Token(TokenType.SLASH, "/"))
            elif op == "%":
                tokens.append(Token(TokenType.PERCENT, "%"))
            elif op == "(":
                tokens.append(Token(TokenType.LPAREN, "("))
            elif op == ")":
                tokens.append(Token(TokenType.RPAREN, ")"))
        tokens.append(Token(TokenType.EOF, ""))
        return tokens

    def lbp(self, token: Token) -> int:
        return TEMPORAL_PRECEDENCE.get(token.type, 0)

    def nud(self, engine: PrattEngine, token: Token) -> float:
        if token.type == TokenType.NUMBER:
            val = float(token.value)
            if engine.current().type == TokenType.UNIT:
                unit_tok = engine.advance()
                return val * TEMPORAL_UNITS[str(unit_tok.value)]
            return val
        elif token.type == TokenType.MINUS:
            return -engine.parse_expression(35)
        elif token.type == TokenType.PLUS:
            return engine.parse_expression(35)
        elif token.type == TokenType.LPAREN:
            val = engine.parse_expression(0)
            if engine.current().type != TokenType.RPAREN:
                raise ValueError("Mismatched parentheses in temporal expression: expected ')'")
            engine.advance()
            if engine.current().type == TokenType.UNIT:
                unit_tok = engine.advance()
                return val * TEMPORAL_UNITS[str(unit_tok.value)]
            return val
        raise ValueError(f"Unexpected token in temporal prefix: {token}")

    def led(self, engine: PrattEngine, token: Token, left: float) -> float:
        lbp = self.lbp(token)
        if token.type == TokenType.PLUS:
            return left + engine.parse_expression(lbp)
        elif token.type == TokenType.MINUS:
            return left - engine.parse_expression(lbp)
        elif token.type == TokenType.STAR:
            return left * engine.parse_expression(lbp)
        elif token.type == TokenType.SLASH:
            right = engine.parse_expression(lbp)
            if right == 0.0:
                raise ZeroDivisionError("Division by zero in temporal calculation")
            return left / right
        elif token.type == TokenType.PERCENT:
            return left % engine.parse_expression(lbp)
        elif token.type == TokenType.POW:
            return left ** engine.parse_expression(lbp - 1)
        elif token.type == TokenType.UNIT:
            return left * TEMPORAL_UNITS[str(token.value)]
        raise ValueError(f"Unexpected token in temporal infix: {token}")


# ==============================================================================
# 2. MathGrammar (Dedicated to Scientific Calculator REPL & Expressions)
# ==============================================================================
MATH_PRECEDENCE = {
    TokenType.PLUS: 10,
    TokenType.MINUS: 10,
    TokenType.STAR: 20,
    TokenType.SLASH: 20,
    TokenType.PERCENT: 20,
    TokenType.POW: 30,
    TokenType.UNIT: 40,
}

MATH_FUNCTIONS: Dict[str, Callable[[float], float]] = {
    "sin": math.sin,
    "cos": math.cos,
    "tan": math.tan,
    "asin": math.asin,
    "acos": math.acos,
    "atan": math.atan,
    "sqrt": math.sqrt,
    "exp": math.exp,
    "log": math.log,
    "log10": math.log10,
    "log2": math.log2,
    "floor": math.floor,
    "ceil": math.ceil,
    "round": round,
    "abs": abs,
}

MATH_CONSTANTS: Dict[str, float] = {
    "pi": math.pi,
    "e": math.e,
    "tau": math.tau,
}


class MathGrammar(Grammar):
    """
    Rich grammar for scientific calculations, variables, and math functions.
    Includes persistent variable table and standard library math functions.
    """

    def __init__(self, variables: Optional[Dict[str, float]] = None):
        self.variables = variables or {}

    def tokenize(self, text: str) -> List[Token]:
        tokens: List[Token] = []
        pattern = re.compile(
            r"(\d+(?:\.\d+)?|\.\d+)|(us|µs|ms|sec|min|s|m|h)\b|([a-zA-Z_][a-zA-Z0-9_]*)|(\*\*|\^)|([+\-*/%(),])"
        )
        for match in pattern.finditer(text):
            num, unit, ident, pow_op, op = match.groups()
            if num is not None:
                tokens.append(Token(TokenType.NUMBER, float(num)))
            elif unit is not None:
                tokens.append(Token(TokenType.UNIT, unit))
            elif ident is not None:
                if ident in TEMPORAL_UNITS and tokens and tokens[-1].type == TokenType.NUMBER:
                    tokens.append(Token(TokenType.UNIT, ident))
                else:
                    tokens.append(Token(TokenType.IDENT, ident))
            elif pow_op is not None:
                tokens.append(Token(TokenType.POW, "**"))
            elif op == "+":
                tokens.append(Token(TokenType.PLUS, "+"))
            elif op == "-":
                tokens.append(Token(TokenType.MINUS, "-"))
            elif op == "*":
                tokens.append(Token(TokenType.STAR, "*"))
            elif op == "/":
                tokens.append(Token(TokenType.SLASH, "/"))
            elif op == "%":
                tokens.append(Token(TokenType.PERCENT, "%"))
            elif op == "(":
                tokens.append(Token(TokenType.LPAREN, "("))
            elif op == ")":
                tokens.append(Token(TokenType.RPAREN, ")"))
            elif op == ",":
                tokens.append(Token(TokenType.COMMA, ","))
        tokens.append(Token(TokenType.EOF, ""))
        return tokens

    def lbp(self, token: Token) -> int:
        return MATH_PRECEDENCE.get(token.type, 0)

    def nud(self, engine: PrattEngine, token: Token) -> float:
        if token.type == TokenType.NUMBER:
            val = float(token.value)
            if engine.current().type == TokenType.UNIT:
                unit_tok = engine.advance()
                return val * TEMPORAL_UNITS[str(unit_tok.value)]
            return val

        elif token.type == TokenType.IDENT:
            name = str(token.value)
            # Function call: sin(x), sqrt(16)
            if engine.current().type == TokenType.LPAREN:
                engine.advance()  # consume '('
                arg = engine.parse_expression(0)
                if engine.current().type != TokenType.RPAREN:
                    raise ValueError(f"Mismatched parentheses in function '{name}': expected ')'")
                engine.advance()  # consume ')'
                if name not in MATH_FUNCTIONS:
                    raise ValueError(f"Unknown math function: '{name}'")
                val = float(MATH_FUNCTIONS[name](arg))
                if engine.current().type == TokenType.UNIT:
                    unit_tok = engine.advance()
                    return val * TEMPORAL_UNITS[str(unit_tok.value)]
                return val

            # Mathematical constant: pi, e, tau
            if name in MATH_CONSTANTS:
                val = MATH_CONSTANTS[name]
                if engine.current().type == TokenType.UNIT:
                    unit_tok = engine.advance()
                    return val * TEMPORAL_UNITS[str(unit_tok.value)]
                return val

            # Persistent user variable: var1, mem
            if name in self.variables:
                val = float(self.variables[name])
                if engine.current().type == TokenType.UNIT:
                    unit_tok = engine.advance()
                    return val * TEMPORAL_UNITS[str(unit_tok.value)]
                return val

            raise ValueError(f"Unknown identifier or variable: '{name}'")

        elif token.type == TokenType.MINUS:
            return -engine.parse_expression(35)
        elif token.type == TokenType.PLUS:
            return engine.parse_expression(35)
        elif token.type == TokenType.LPAREN:
            val = engine.parse_expression(0)
            if engine.current().type != TokenType.RPAREN:
                raise ValueError("Mismatched parentheses: expected ')'")
            engine.advance()
            if engine.current().type == TokenType.UNIT:
                unit_tok = engine.advance()
                return val * TEMPORAL_UNITS[str(unit_tok.value)]
            return val

        raise ValueError(f"Unexpected token in math prefix: {token}")

    def led(self, engine: PrattEngine, token: Token, left: float) -> float:
        lbp = self.lbp(token)
        if token.type == TokenType.PLUS:
            return left + engine.parse_expression(lbp)
        elif token.type == TokenType.MINUS:
            return left - engine.parse_expression(lbp)
        elif token.type == TokenType.STAR:
            return left * engine.parse_expression(lbp)
        elif token.type == TokenType.SLASH:
            right = engine.parse_expression(lbp)
            if right == 0.0:
                raise ZeroDivisionError("Division by zero in math expression")
            return left / right
        elif token.type == TokenType.PERCENT:
            return left % engine.parse_expression(lbp)
        elif token.type == TokenType.POW:
            return left ** engine.parse_expression(lbp - 1)
        elif token.type == TokenType.UNIT:
            return left * TEMPORAL_UNITS[str(token.value)]
        raise ValueError(f"Unexpected token in math infix: {token}")


# ==============================================================================
# Helper Facades & Backward Compatibility
# ==============================================================================
_DEFAULT_TEMPORAL_GRAMMAR = TemporalGrammar()


def evaluate_temporal(expr: str) -> float:
    """Evaluates an expression using TemporalGrammar (safe for clock & timer intervals)."""
    tokens = _DEFAULT_TEMPORAL_GRAMMAR.tokenize(expr)
    if not tokens or tokens[0].type == TokenType.EOF:
        return 0.0
    return PrattEngine(tokens, _DEFAULT_TEMPORAL_GRAMMAR).parse()


def evaluate_math(expr: str, variables: Optional[Dict[str, float]] = None) -> float:
    """Evaluates an expression using MathGrammar (functions, constants, variables)."""
    grammar = MathGrammar(variables=variables)
    tokens = grammar.tokenize(expr)
    if not tokens or tokens[0].type == TokenType.EOF:
        return 0.0
    return PrattEngine(tokens, grammar).parse()


def evaluate_expression(expr: str, variables: Optional[Dict[str, float]] = None) -> float:
    """General expression evaluator (defaults to MathGrammar)."""
    return evaluate_math(expr, variables=variables)


def interpolate_template(
    template: str,
    grammar: Optional[Grammar] = None,
    variables: Optional[Dict[str, float]] = None,
) -> str:
    """
    Interpolates bracketed expressions '[...]' within a template string.
    Example:
        'Score: [5 * 6] | Sin: [sin(pi / 2)]' -> 'Score: 30 | Sin: 1'
    """
    bracket_pattern = re.compile(r"\[(.*?)\]")
    active_grammar = grammar or MathGrammar(variables=variables)

    def replacer(match: re.Match) -> str:
        expr = match.group(1).strip()
        tokens = active_grammar.tokenize(expr)
        if not tokens or tokens[0].type == TokenType.EOF:
            return "0"
        val = PrattEngine(tokens, active_grammar).parse()
        if isinstance(val, float) and val.is_integer():
            return str(int(val))
        return f"{val:.4f}".rstrip("0").rstrip(".")

    return bracket_pattern.sub(replacer, template)
