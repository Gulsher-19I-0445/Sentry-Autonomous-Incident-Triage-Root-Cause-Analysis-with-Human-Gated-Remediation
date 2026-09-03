"""Combine several harness runs into one report.

Scenarios are run at different repetition counts — the adversarial set needs
n=3 because the answer is a judgment call and sampling moves it, while the
genuine set has been stable at 1.0 across four sweeps and does not. That means
separate invocations, and separate result files.

Rates like false attribution and calibration are only meaningful pooled across
everything, so this re-derives the summary from the union rather than averaging
two averages, which would silently weight the smaller run more heavily.

    python merge.py eval-adversarial.json eval-genuine.json -o eval-baseline.json
"""

import argparse
import json
from pathlib import Path

from scoring import Result, summarize, variance_by_scenario


def load(path: Path) -> list[Result]:
    report = json.loads(path.read_text())
    results = []
    for row in report.get("results", []):
        # Result gained fields over time; ignore any a older file predates
        # rather than failing to load it.
        known = {k: v for k, v in row.items() if k in Result.__dataclass_fields__}
        results.append(Result(**known))
    return results


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("files", nargs="+", type=Path)
    parser.add_argument("-o", "--out", type=Path, default=Path("eval-merged.json"))
    args = parser.parse_args()

    results: list[Result] = []
    for path in args.files:
        loaded = load(path)
        print(f"  {path.name}: {len(loaded)} runs")
        results.extend(loaded)

    seen = {(r.scenario_id, r.run_index) for r in results}
    if len(seen) != len(results):
        print("  WARNING: the same scenario and run index appears in more than "
              "one file — merging them double-counts those runs")

    report = {
        "generated_at": None,
        "merged_from": [str(p) for p in args.files],
        "summary": summarize(results),
        "by_scenario": variance_by_scenario(results),
        "results": [r.__dict__ for r in results],
    }
    args.out.write_text(json.dumps(report, indent=2))

    print(f"\n  {len(results)} runs -> {args.out}\n")
    summary = report["summary"]
    if "error" in summary:
        print(f"  {summary['error']}")
        return 1
    for key, value in summary.items():
        if key != "calibration":
            print(f"  {key:28} {value}")
    print("\n  calibration:")
    for key, value in summary["calibration"].items():
        print(f"    {key:26} {value}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
