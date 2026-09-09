"""Report each step's runtime against its own history.

The first version of this compared runtimes to their timeout caps, which was
backwards: it only warned usefully if the caps were tight, so it pushed toward
keeping caps close to observed runtimes -- the very thing that made ACRIS fail
three times and distress signals twice.

Caps and drift detection are separate jobs. A cap is a circuit breaker: it
should be generous and exists to stop a runaway, not to measure anything. Drift
is a comparison against what the step used to take. A step going from 8.7 to
16.2 minutes is worth knowing about whether its cap is 20 minutes or 60.

So this compares each step to the median of its recent runs and says something
when that changes materially. The cap is still printed, as context rather than
as the yardstick.
"""

import json
import os
import re
import statistics
import sys
import urllib.error
import urllib.request
from datetime import datetime
from pathlib import Path

WORKFLOW = Path(__file__).parent.parent / ".github/workflows/refresh-data.yml"
HISTORY_RUNS = 10       # how far back to look for a baseline
JUMP = 1.5              # flag a step this many times slower than its median
FLOOR_MINUTES = 1.0     # below this, percentage changes are noise
API = "https://api.github.com"


def _get(url):
    token = os.environ.get("GH_TOKEN") or os.environ.get("GITHUB_TOKEN")
    req = urllib.request.Request(url, headers={
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github+json",
    })
    with urllib.request.urlopen(req, timeout=30) as resp:
        return json.loads(resp.read())


def caps_from_workflow() -> dict[str, int]:
    """Step name -> timeout-minutes, read back out of the workflow file so this
    cannot drift from the thing it is describing."""
    if not WORKFLOW.exists():
        return {}
    caps, name = {}, None
    for line in WORKFLOW.read_text().splitlines():
        m = re.match(r"\s*-?\s*name:\s*(.+?)\s*$", line)
        if m:
            name = m.group(1).strip().strip('"\'')
        m = re.match(r"\s*timeout-minutes:\s*(\d+)", line)
        if m and name:
            caps[name] = int(m.group(1))
    return caps


def minutes(step):
    if not (step.get("started_at") and step.get("completed_at")):
        return None
    fmt = "%Y-%m-%dT%H:%M:%SZ"
    a = datetime.strptime(step["started_at"], fmt)
    b = datetime.strptime(step["completed_at"], fmt)
    return (b - a).total_seconds() / 60


def steps_of(run_id, repo):
    out = {}
    for job in _get(f"{API}/repos/{repo}/actions/runs/{run_id}/jobs").get("jobs", []):
        for s in job.get("steps", []):
            m = minutes(s)
            if m is not None:
                out[s["name"]] = m
    return out


def main() -> int:
    repo = os.environ.get("GITHUB_REPOSITORY")
    run_id = os.environ.get("GITHUB_RUN_ID")
    workflow = os.environ.get("GITHUB_WORKFLOW")
    if not (repo and run_id):
        print("Not running in Actions; skipping budget report.")
        return 0

    current = steps_of(run_id, repo)
    if not current:
        print("No step data available; skipping budget report.")
        return 0

    # Baseline from earlier runs of this workflow. A step can complete fine in a
    # run that later failed, so every finished step counts, not only green runs.
    history: dict[str, list[float]] = {}
    try:
        runs = _get(f"{API}/repos/{repo}/actions/runs?per_page={HISTORY_RUNS + 5}")
        for r in runs.get("workflow_runs", []):
            if str(r["id"]) == str(run_id) or r["status"] != "completed":
                continue
            if workflow and r.get("name") != workflow:
                continue
            for name, mins in steps_of(r["id"], repo).items():
                history.setdefault(name, []).append(mins)
            if len(next(iter(history.values()), [])) >= HISTORY_RUNS:
                break
    except (urllib.error.URLError, OSError, KeyError, ValueError) as e:
        print(f"(no history available: {type(e).__name__})")

    caps = caps_from_workflow()
    rows, flags = [], []
    for name, mins in current.items():
        past = history.get(name, [])
        base = statistics.median(past) if past else None
        ratio = (mins / base) if base and base > 0 else None
        rows.append((name, mins, base, ratio, caps.get(name)))
        if ratio and ratio >= JUMP and mins >= FLOOR_MINUTES:
            flags.append((name, mins, base, ratio, caps.get(name)))

    width = max((len(r[0]) for r in rows), default=10)
    head = f"{'step'.ljust(width)}  {'mins':>6}  {'was':>6}  {'change':>7}  {'cap':>5}"
    lines = [head, "-" * len(head)]
    for name, mins, base, ratio, cap in rows:
        lines.append(
            f"{name.ljust(width)}  {mins:6.1f}  "
            f"{(f'{base:.1f}' if base else '-'):>6}  "
            f"{(f'{(ratio - 1) * 100:+.0f}%' if ratio else '-'):>7}  "
            f"{(str(cap) if cap else '-'):>5}"
        )
    report = "\n".join(lines)
    print(report)

    # If most steps slowed down at once it is the day, not the code. Saying so
    # once is more use than fifteen identical warnings about unrelated sources.
    comparable = [r for r in rows if r[3] is not None and r[1] >= FLOOR_MINUTES]
    systemic = len(comparable) >= 3 and len(flags) >= 0.6 * len(comparable)

    if systemic:
        med = statistics.median([r[3] for r in flags])
        print(f"::warning title=Everything is slow::{len(flags)} of {len(comparable)} steps "
              f"are at least {JUMP:g}x their median (typically {(med - 1) * 100:+.0f}%). "
              f"A run-wide slowdown is the data source or the runner having a bad day, "
              f"not a regression in any one step. Worth re-checking on the next run "
              f"before changing anything.")
    else:
        for name, mins, base, ratio, cap in flags:
            print(f"::warning title=Step slowing down::'{name}' took {mins:.1f}m against a "
                  f"median of {base:.1f}m ({(ratio - 1) * 100:+.0f}%)"
                  + (f", cap {cap}m" if cap else "") +
                  ". Worth a look before it becomes a failure.")

    summary = os.environ.get("GITHUB_STEP_SUMMARY")
    if summary:
        with open(summary, "a") as fh:
            fh.write("## Step runtimes vs recent history\n\n```\n" + report + "\n```\n")
            if systemic:
                fh.write(f"\n{len(flags)} of {len(comparable)} steps slowed down at once — "
                         f"read this as a slow day, not a regression.\n")
            elif flags:
                fh.write(f"\n{len(flags)} step(s) at least {JUMP:g}x their median.\n")
            else:
                fh.write("\nNothing slowing down materially.\n")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except (urllib.error.URLError, OSError, KeyError, ValueError) as e:
        # Reporting must never be the reason a refresh fails.
        print(f"Budget report unavailable: {type(e).__name__}: {e}")
        sys.exit(0)
