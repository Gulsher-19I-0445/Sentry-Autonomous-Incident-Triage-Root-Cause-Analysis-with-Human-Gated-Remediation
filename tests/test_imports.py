"""Import-time integrity.

Every bug this file catches previously cost a full deploy cycle, because the
failure only appeared at Lambda cold start:

    KeyError: 'TABLE_NAME'                       env var read at import time
    No module named 'sentry.agent.tools.init'    wrong relative import depth
    'tuple' object has no attribute 'converse'   trailing comma after boto3.client(...)

The module list is discovered rather than hardcoded, so a module added later is
covered without anyone remembering to add it here.
"""

import ast
import importlib
import pkgutil
from pathlib import Path

import pytest
from botocore.client import BaseClient

SRC = Path(__file__).resolve().parent.parent / "src"


def _all_modules() -> list[str]:
    """Every module under src/, both deployment bundles."""
    found = []
    for package in ("sentry", "target_app"):
        pkg_path = SRC / package
        found.append(package)
        for info in pkgutil.walk_packages([str(pkg_path)], prefix=f"{package}."):
            found.append(info.name)
    return sorted(found)


MODULES = _all_modules()


def test_module_discovery_found_both_bundles():
    """Guards the guard: an empty list would make every test below vacuous."""
    assert any(m.startswith("sentry.") for m in MODULES)
    assert any(m.startswith("target_app.") for m in MODULES)
    assert len(MODULES) >= 15, f"suspiciously few modules discovered: {MODULES}"


@pytest.mark.parametrize("module_name", MODULES)
def test_module_imports(module_name):
    """Import every module exactly as the Lambda runtime would."""
    importlib.import_module(module_name)


@pytest.mark.parametrize("module_name", MODULES)
def test_no_client_is_a_tuple(module_name):
    """A trailing comma after boto3.client(...) makes a one-element tuple.

    It imports fine and fails at first use with 'tuple' object has no attribute
    'converse' — in production, mid-investigation. Cheap to catch here.
    """
    module = importlib.import_module(module_name)
    for attr_name in dir(module):
        value = getattr(module, attr_name, None)
        if isinstance(value, tuple) and any(isinstance(v, BaseClient) for v in value):
            pytest.fail(
                f"{module_name}.{attr_name} is a tuple containing a boto3 client "
                f"— check for a trailing comma after boto3.client(...)"
            )


def test_bedrock_client_is_a_client():
    """The specific instance of the above that already bit once."""
    from sentry.agent import bedrock

    assert not isinstance(bedrock._client, tuple)
    assert isinstance(bedrock._client, BaseClient)
    assert callable(getattr(bedrock._client, "converse", None))


def test_bedrock_client_does_not_retry_internally():
    """botocore's own retry once invoked the agent twice for a single incident:
    the 60s read timeout fired, botocore retried, and two investigations ran."""
    from sentry.agent import bedrock

    config = bedrock._client.meta.config
    assert config.read_timeout >= 60, "read timeout too low — Converse calls are slow"
    # botocore normalises max_attempts=0 into total_max_attempts=1, i.e. one
    # attempt and no retry. Asserting on the normalised key is what actually
    # pins the behaviour.
    assert config.retries.get("total_max_attempts") == 1, (
        f"botocore retries must be off (got {config.retries}); retrying a Converse "
        f"call duplicates the whole investigation"
    )


def test_agent_handler_exposes_tools_from_the_registry():
    """handler.TOOLS is built at import time from specs(); an empty list means
    the agent runs with no evidence and escalates everything."""
    from sentry.agent import handler

    assert isinstance(handler.TOOLS, list)
    assert len(handler.TOOLS) == 4, f"expected 4 registered tools, got {len(handler.TOOLS)}"


BANNED = ("chaos", "fault_injection", "inject_failure", "simulate_failure",
          "fault injection", "injected")


def _log_message_literals(tree: ast.AST) -> list[tuple[int, str]]:
    """The message argument of every log_event(...) / logger.x(...) call.

    Scoped deliberately. Comments and docstrings never reach CloudWatch, so
    grepping whole files produces false positives on the very comments that
    explain the rule. What reaches the agent is the message string, the module
    and function names in a traceback, and the source line a traceback renders.
    """
    found = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        target = node.func
        name = target.attr if isinstance(target, ast.Attribute) else getattr(target, "id", "")

        if name == "log_event":
            args = node.args[2:3]          # log_event(logger, level, message, ...)
        elif name in ("info", "warning", "error", "debug", "exception", "critical"):
            args = node.args[:1]
        else:
            continue

        for arg in args:
            for sub in ast.walk(arg):
                if isinstance(sub, ast.Constant) and isinstance(sub.value, str):
                    found.append((node.lineno, sub.value))
    return found


@pytest.mark.parametrize("path", sorted((SRC / "target_app").rglob("*.py")),
                         ids=lambda p: p.name)
def test_target_app_log_messages_never_reveal_fault_injection(path):
    """Everything target_app writes to stdout lands in a log group the agent
    queries. A message saying the failure was injected makes the model report
    the harness instead of the incident — which already invalidated one
    evaluation run, and is worth failing the build over."""
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))

    for lineno, literal in _log_message_literals(tree):
        lowered = literal.lower()
        for word in BANNED:
            assert word not in lowered, (
                f"{path.name}:{lineno} logs {literal!r}, which reaches "
                f"/aws/lambda/sentry-capstone-*-gulsher — the agent reads that "
                f"group as primary evidence"
            )


@pytest.mark.parametrize("path", sorted((SRC / "target_app").rglob("*.py")),
                         ids=lambda p: p.name)
def test_target_app_symbol_names_never_reveal_fault_injection(path):
    """Module, class and function names appear in every stack trace. The
    injection module is `_internal.py` with `_process_request`/`_apply` for
    exactly this reason."""
    assert not any(w in path.stem.lower() for w in BANNED), (
        f"module name {path.name} appears in every traceback from it"
    )

    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            lowered = node.name.lower()
            for word in BANNED:
                assert word not in lowered, (
                    f"{path.name}:{node.lineno} defines {node.name!r}; it will "
                    f"appear in any stack trace passing through it"
                )
