import ast
import logging
import math
import operator

import discord
from discord import app_commands
from discord.ext import commands

import embeds

log = logging.getLogger(__name__)

EXPR_LIMIT = 200
POW_LIMIT = 1000
FACT_LIMIT = 1000
DIGIT_LIMIT = 5000
SHOW_LIMIT = 1000


class CalcError(Exception):
    pass


_BIN_OPS = {
    ast.Add: operator.add,
    ast.Sub: operator.sub,
    ast.Mult: operator.mul,
    ast.Div: operator.truediv,
    ast.FloorDiv: operator.floordiv,
    ast.Mod: operator.mod,
    ast.Pow: operator.pow,
}

_UNARY_OPS = {
    ast.UAdd: operator.pos,
    ast.USub: operator.neg,
}

_FUNCTIONS = {
    "abs": abs,
    "round": round,
    "min": min,
    "max": max,
    "sqrt": math.sqrt,
    "floor": math.floor,
    "ceil": math.ceil,
    "log": math.log,
    "log10": math.log10,
    "factorial": math.factorial,
}

_CONSTANTS = {
    "pi": math.pi,
    "e": math.e,
    "tau": math.tau,
}


def _guard_pow(base, exp):
    if not (isinstance(base, (int, float)) and isinstance(exp, (int, float))):
        return
    if abs(exp) > POW_LIMIT:
        raise CalcError("that exponent is too large.")
    b = abs(base)
    if b > 1 and exp > 0:
        try:
            digits = exp * math.log10(b)
        except (ValueError, OverflowError):
            raise CalcError("that result is too large.")
        if digits > DIGIT_LIMIT:
            raise CalcError("that result is too large.")


def _eval(node):
    if isinstance(node, ast.Expression):
        return _eval(node.body)

    if isinstance(node, ast.Constant):
        if isinstance(node.value, bool) or not isinstance(node.value, (int, float)):
            raise CalcError("only numbers are allowed.")
        return node.value

    if isinstance(node, ast.BinOp):
        op = _BIN_OPS.get(type(node.op))
        if op is None:
            raise CalcError("that operator isn't supported.")
        left = _eval(node.left)
        right = _eval(node.right)
        if isinstance(node.op, ast.Pow):
            _guard_pow(left, right)
        return op(left, right)

    if isinstance(node, ast.UnaryOp):
        op = _UNARY_OPS.get(type(node.op))
        if op is None:
            raise CalcError("that operator isn't supported.")
        return op(_eval(node.operand))

    if isinstance(node, ast.Call):
        if not isinstance(node.func, ast.Name):
            raise CalcError("that isn't a function i know.")
        fn = _FUNCTIONS.get(node.func.id)
        if fn is None:
            raise CalcError(f"`{node.func.id}` isn't a function i know.")
        if node.keywords:
            raise CalcError("functions don't take named arguments here.")
        args = [_eval(arg) for arg in node.args]
        if node.func.id == "factorial":
            if args and isinstance(args[0], (int, float)) and args[0] > FACT_LIMIT:
                raise CalcError("that factorial is too large.")
        return fn(*args)

    if isinstance(node, ast.Name):
        if node.id in _CONSTANTS:
            return _CONSTANTS[node.id]
        raise CalcError(f"`{node.id}` isn't something i recognise.")

    raise CalcError("i couldn't read that expression.")


def calculate(expression):
    expr = (expression or "").strip()
    if not expr:
        raise CalcError("give me something to calculate, like `2 + 2 * 5`.")
    if len(expr) > EXPR_LIMIT:
        raise CalcError("that expression is too long.")

    expr = expr.replace("×", "*").replace("÷", "/").replace("^", "**")

    try:
        tree = ast.parse(expr, mode="eval")
    except SyntaxError:
        raise CalcError("i couldn't parse that. use numbers and `+ - * / ( )`.")

    try:
        return _eval(tree)
    except CalcError:
        raise
    except ZeroDivisionError:
        raise CalcError("i can't divide by zero.")
    except (OverflowError, ValueError):
        raise CalcError("that number is out of range for me.")
    except Exception:
        raise CalcError("i couldn't work that out.")


def format_number(value):
    if isinstance(value, int):
        return str(value)
    if isinstance(value, float):
        if math.isnan(value) or math.isinf(value):
            return str(value)
        rounded = round(value, 10)
        if rounded == int(rounded) and abs(rounded) < 1e16:
            return str(int(rounded))
        return f"{rounded:.10g}"
    return str(value)


class Calculator(commands.Cog):
    """Work out a math expression."""

    def __init__(self, bot):
        self.bot = bot

    async def cog_command_error(self, ctx, error):
        error = getattr(error, "original", error)
        if isinstance(error, commands.MissingRequiredArgument):
            await embeds.send(
                ctx,
                embeds.error("give me something to calculate, like `2 + 2 * 5`."),
            )
        elif isinstance(error, commands.CommandOnCooldown):
            await embeds.send(
                ctx,
                embeds.error(
                    f"slow down, try again in {error.retry_after:.0f}s.",
                    title="Slow down",
                ),
            )
        else:
            log.exception("Unhandled error in %s", ctx.command, exc_info=error)
            await embeds.send(
                ctx, embeds.error("something broke on my end. it has been logged.")
            )

    @commands.hybrid_command(
        name="calculator",
        aliases=["calc", "math"],
        description="Work out a math expression.",
    )
    @app_commands.describe(expression="the expression to calculate")
    @commands.cooldown(1, 3, commands.BucketType.user)
    async def calculator(self, ctx, *, expression: str):
        try:
            result = calculate(expression)
        except CalcError as problem:
            await embeds.send(ctx, embeds.error(str(problem)))
            return

        shown = format_number(result)
        if len(shown) > SHOW_LIMIT:
            await embeds.send(ctx, embeds.error("that answer is too big to show."))
            return

        clean = expression.strip()[:EXPR_LIMIT].replace("`", "")
        await embeds.send(
            ctx,
            embeds.build(f"`{clean}` = **{shown}**"),
        )


async def setup(bot):
    await bot.add_cog(Calculator(bot))