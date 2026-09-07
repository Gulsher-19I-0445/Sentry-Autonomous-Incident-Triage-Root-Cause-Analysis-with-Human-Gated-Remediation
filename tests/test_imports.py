"""Import-time integrity.

Every bug this file catches previously cost a full deploy cycle, because the
failure only appeared at Lambda cold start:

    KeyError: 'TABLE_NAME'                       env var read at import time
    No module named 'sentry.agent.tools.init'    wrong relative import depth
    'tuple' object has no attribute 'converse'   trailing comma after boto3.client(...)

The module list is discovered rather than hardcoded, so a module added later is
covered without anyone remembering to add it here.
"""

import importlib
import pkgutil
from pathlib import Path

import pytest
from botocore.client import BaseClient

SRC = Path(__file__).resolve().parent.parent / "src"


def _all_modules() -> list[str]:
    """Every module in the deployment bundle."""
    found = ["sentry"]
    for info in pkgutil.walk_packages([str(SRC / "sentry")], prefix="sentry."):
        found.append(info.name)
    return sorted(found)


MODULES = _all_modules()


def test_module_discovery_found_the_bundle():
    """Guards the guard: an empty list would make every test below vacuous."""
    assert any(m.startswith("sentry.") for m in MODULES)
    assert len(MODULES) >= 12, f"suspiciously few modules discovered: {MODULES}"


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
