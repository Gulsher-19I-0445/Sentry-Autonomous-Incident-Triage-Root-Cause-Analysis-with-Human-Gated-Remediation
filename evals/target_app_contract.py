"""What the evaluated application offers, mirrored.

The application under test lives in its own repository now, so this cannot be
imported from it. That separation is deliberate — the agent reads that
repository's commits as evidence, and it must not also contain the answers.

The cost of the separation is this file: a copy, and copies drift. Two things
limit the damage.

  * Offline, `test_scenarios.py` checks every scenario against this mapping, so
    a scenario naming a mode that was never real still fails in CI.
  * At run time, `harness.check_modes_against_deployment()` asks the deployed
    application what it actually offers and fails the sweep on a mismatch.

The second is the one that catches genuine drift, because it compares against
reality rather than against another copy. Without it, a mode renamed in the
other repository would arm nothing, every scenario using it would report "did
not reproduce", and the sweep would look like a finding instead of a fault.

`component` is where the fault fires, which is not always where it surfaces —
see CROSS_COMPONENT in test_scenarios.py.
"""

MODES: dict[str, dict[str, str]] = {
    "exception":   {"component": "api"},
    "slow":        {"component": "api"},
    "bad_payload": {"component": "api"},
    "memory":      {"component": "consumer"},
    "timeout":     {"component": "consumer"},
    "denied":      {"component": "consumer"},
    "missing_env": {"component": "consumer"},
    "retry_storm": {"component": "consumer"},
    "silent":      {"component": "consumer"},
}
