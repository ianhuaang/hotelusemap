"""Report how close each workflow step came to its timeout cap.

Every cap in refresh-data.yml was originally set just above whatever the step
happened to take that week, so each one eventually failed: ACRIS went 10m ->
25m -> 45m, distress signals 15m -> 45m. The failure mode is always the same
and always invisible until the run dies, because a step at 44 minutes and a
step at 45 look identical in the UI.

This prints the ratio. A step consistently above WARN_AT is going to fail
sooner or later, and the point is to see that weeks ahead rather than on the
morning someone needs the data.

Caps are read back out of the workflow file rather than duplicated here, so
this cannot drift out of sync with the thing it is measuring.
"""

import json
import os
import re
import sys
import urllib.error
import urllib.request
from datetime import datetime
from pathlib import Path

WORKFLOW = Path(__file__).parent.parent / ".github/workflows/refresh-data.yml"
WARN_AT = 0.70


def caps_from_workflow() -> dict[str, int]:
    """Map step name -> timeout-minutes, parsed from the workflow YAML."""
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


def fetch_steps() -> list[dict]:
    run_id = os.environ.get("GITHUB_RUN_ID")
    repo = os.environ.get("GITHUB_REPOSITORY")
    token = os.environ.get("GH_TOKEN") or os.environ.get("GITHUB_TOKEN")
    if not (run_id and repo and token):
        return []
    url = f"https://api.github.com/repos/{repo}/actions/runs/{run_id}/jobs"
    req = urllib.request.Request(url, headers={
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github+json",
    })
    with urllib.request.urlopen(req, timeout=30) as resp:
        jobs = json.loads(resp.read())["jobs"]
    return [s for j in jobs for s in j.get("steps", [])]


def main() -> int:
    caps = caps_from_workflow()
    steps = fetch_steps()
    if not steps:
        print("No step data available; skipping budget report.")
        return 0

    parse = lambda t: datetime.strptime(t, "%Y-%m-%dT%H:%M:%SZ")
    rows, warnings = [], []
    for s in steps:
        if not (s.get("started_at") and s.get("completed_at")):
            continue
        mins = (parse(s["completed_at"]) - parse(s["started_at"])).total_seconds() / 60
        cap = caps.get(s["name"])
        pct = mins / cap if cap else None
        rows.append((s["name"], mins, cap, pct, s.get("conclusion", "?")))
        if pct is not None and pct >= WARN_AT:
            warnings.append((s["name"], mins, cap, pct))

    width = max((len(r[0]) for r in rows), default=10)
    lines = [f"{'step'.ljust(width)}  {'mins':>6}  {'cap':>5}  {'used':>6}"]
    lines.append("-" * len(lines[0]))
    for name, mins, cap, pct, _ in rows:
        lines.append(
            f"{name.ljust(width)}  {mins:6.1f}  {(str(cap) if cap else '-'):>5}  "
            f"{(f'{pct*100:.0f}%' if pct is not None else '-'):>6}"
        )
    report = "\n".join(lines)
    print(report)

    for name, mins, cap, pct in warnings:
        print(f"::warning title=Approaching timeout::'{name}' used {mins:.1f} of "
              f"its {cap}m cap ({pct*100:.0f}%). Raising the cap buys weeks; making "
              f"the step's work proportional to our building list fixes it.")

    summary = os.environ.get("GITHUB_STEP_SUMMARY")
    if summary:
        with open(summary, "a") as fh:
            fh.write("## Step runtimes vs caps\n\n```\n" + report + "\n```\n")
            if warnings:
                fh.write(f"\n{len(warnings)} step(s) above {WARN_AT*100:.0f}% of cap.\n")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except (urllib.error.URLError, OSError, KeyError, ValueError) as e:
        # Reporting must never be the reason a refresh fails.
        print(f"Budget report unavailable: {type(e).__name__}: {e}")
        sys.exit(0)
