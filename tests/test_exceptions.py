"""Every class that gets raised must actually be raisable.

`class IllegalTransition: pass` imports fine, passes review, and blows up as

    TypeError: exceptions must derive from BaseException

only when the raise statement executes — which for an error path means the first
time something goes wrong in production, replacing a useful error with a
confusing one.

Rather than listing known exception classes, this scans the source for every
`raise Something(...)` and resolves the name against the module that raises it.
That covers classes added later without anyone updating this file.
"""

import ast
import importlib
from pathlib import Path

import pytest

SRC = Path(__file__).resolve().parent.parent / "src"


def _module_name(path: Path) -> str:
    rel = path.relative_to(SRC).with_suffix("")
    parts = list(rel.parts)
    if parts[-1] == "__init__":
        parts.pop()
    return ".".join(parts)


def _raised_names() -> list[tuple[str, str, int]]:
    """(module, dotted name raised, line number) for every `raise Name(...)`.

    Bare `raise` and `raise variable` are skipped — neither names a class.
    """
    found = []
    for path in sorted(SRC.rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        module = _module_name(path)
        for node in ast.walk(tree):
            if not isinstance(node, ast.Raise) or node.exc is None:
                continue
            call = node.exc
            if not isinstance(call, ast.Call):
                continue
            target = call.func
            if isinstance(target, ast.Name):
                found.append((module, target.id, node.lineno))
            elif isinstance(target, ast.Attribute):
                bits = []
                cur = target
                while isinstance(cur, ast.Attribute):
                    bits.append(cur.attr)
                    cur = cur.value
                if isinstance(cur, ast.Name):
                    bits.append(cur.id)
                    found.append((module, ".".join(reversed(bits)), node.lineno))
    return found


RAISED = _raised_names()


def test_scan_found_raise_statements():
    """If the scan silently found nothing, everything below passes for free."""
    assert len(RAISED) >= 5, f"expected several raise statements, found {RAISED}"


@pytest.mark.parametrize(
    "module_name,raised,lineno",
    RAISED,
    ids=[f"{m}:{n}:{ln}" for m, n, ln in RAISED],
)
def test_raised_class_derives_from_exception(module_name, raised, lineno):
    import builtins

    module = importlib.import_module(module_name)

    parts = raised.split(".")
    # Builtins (ValueError, RuntimeError) are not module attributes but are
    # perfectly valid to raise — resolve them rather than skipping.
    obj = module if hasattr(module, parts[0]) else builtins
    for part in parts:
        obj = getattr(obj, part, None)
        if obj is None:
            break

    if obj is None:
        # Not resolvable at module scope — a locally defined or shadowed name.
        # Nothing to assert, and guessing would produce false failures.
        pytest.skip(f"{raised} is not resolvable at module scope")

    assert isinstance(obj, type), f"{module_name}:{lineno} raises non-class {raised}"
    assert issubclass(obj, BaseException), (
        f"{module_name}:{lineno} raises {raised}, which does not subclass "
        f"Exception — `raise` will fail with TypeError instead of raising it"
    )


def test_named_error_classes_derive_from_exception():
    """Complements the raise-scan: a class that looks like an exception but is
    never raised in this repo (yet) is still a trap for the next caller."""
    import pkgutil

    suspects = []
    for package in ("sentry",):
        for info in pkgutil.walk_packages([str(SRC / package)], prefix=f"{package}."):
            module = importlib.import_module(info.name)
            for attr in dir(module):
                value = getattr(module, attr, None)
                if not isinstance(value, type) or value.__module__ != info.name:
                    continue
                looks_like_error = (
                    attr.endswith(("Error", "Exception"))
                    or "Invalid" in attr
                    or "Illegal" in attr
                    or "Duplicate" in attr
                    or "Exceeded" in attr
                )
                if looks_like_error and not issubclass(value, BaseException):
                    suspects.append(f"{info.name}.{attr}")

    assert not suspects, f"named like exceptions but not raisable: {suspects}"


def test_invalid_rca_is_a_value_error():
    """handler._run_investigation catches (InvalidRCA, ValueError) to trigger the
    repair path. If InvalidRCA stopped being a ValueError the repair still works,
    but extract_json's ValueError must stay caught by the same clause."""
    from sentry.agent.schema import InvalidRCA

    assert issubclass(InvalidRCA, ValueError)


def test_max_steps_exceeded_carries_the_partial_run():
    """The escalation path records what the agent managed to do before giving up."""
    from sentry.agent.bedrock import AgentRun, MaxStepsExceeded

    run = AgentRun(model_id="us.anthropic.claude-sonnet-5")
    exc = MaxStepsExceeded("gave up", run=run)

    assert exc.run is run
    assert str(exc) == "gave up"
    # The handler raises it with one argument in the repair path.
    assert MaxStepsExceeded("no output").run is None
