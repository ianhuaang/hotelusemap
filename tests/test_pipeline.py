"""Pipeline ground-truth tests.

These tests run the pipeline against real data and assert that ground-truth
buildings are classified correctly. They fail loudly if the pipeline regresses.
"""

import csv
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))
sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from src.pipeline import run_pipeline
from config import TIER_LEGAL_TRANSIENT, TIER_CLASS_B, TIER_PRIOR_OPERATOR

PROJECT_ROOT = Path(__file__).parent.parent
GROUND_TRUTH = PROJECT_ROOT / "ground_truth.csv"


def _load_ground_truth():
    with open(GROUND_TRUTH) as f:
        return list(csv.DictReader(f))


def _get_pipeline_results():
    """Run pipeline or load cached results."""
    results = run_pipeline()
    return {r["bbl"]: r for r in results}


def test_eligibility_positives_surface_in_top_tiers():
    """Eligibility positives MUST appear as legal_transient or class_b."""
    by_bbl = _get_pipeline_results()
    gt = _load_ground_truth()

    top_tiers = {TIER_LEGAL_TRANSIENT, TIER_CLASS_B}
    failures = []
    for row in gt:
        if row["label_type"] != "eligibility_positive":
            continue
        bbl = row["bbl"]
        result = by_bbl.get(bbl)
        if result is None:
            failures.append(f"{row['name']} (BBL {bbl}): not in pipeline output")
        elif result["tier"] not in top_tiers:
            failures.append(f"{row['name']} (BBL {bbl}): tier={result['tier']}, expected one of {top_tiers}")

    assert not failures, "Eligibility positives failed:\n" + "\n".join(failures)


def test_prior_operators_appear_in_pipeline():
    """Prior operators must appear somewhere in the pipeline output."""
    by_bbl = _get_pipeline_results()
    gt = _load_ground_truth()

    failures = []
    for row in gt:
        if row["label_type"] != "prior_operator":
            continue
        bbl = row["bbl"]
        result = by_bbl.get(bbl)
        if result is None:
            failures.append(f"{row['name']} (BBL {bbl}): not in pipeline output at all")
        elif result.get("prior_operator") is None and result["tier"] != TIER_PRIOR_OPERATOR:
            failures.append(f"{row['name']} (BBL {bbl}): no prior_operator info and tier={result['tier']}")

    assert not failures, "Prior operators failed:\n" + "\n".join(failures)


def test_negatives_not_in_top_tiers():
    """Negatives must NOT appear as legal_transient or class_b."""
    by_bbl = _get_pipeline_results()
    gt = _load_ground_truth()

    top_tiers = {TIER_LEGAL_TRANSIENT, TIER_CLASS_B}
    failures = []
    for row in gt:
        if row["label_type"] != "negative":
            continue
        bbl = row["bbl"]
        result = by_bbl.get(bbl)
        if result is not None and result["tier"] in top_tiers:
            failures.append(f"{row['name']} (BBL {bbl}): tier={result['tier']}, should NOT be in top tiers")

    assert not failures, "Negatives surfaced incorrectly:\n" + "\n".join(failures)


def test_pipeline_produces_reasonable_counts():
    """Sanity check: pipeline should produce between 1k-50k buildings for Manhattan."""
    results = run_pipeline()
    assert 1000 < len(results) < 50000, f"Pipeline produced {len(results)} buildings — out of expected range"


def test_tier_distribution_sanity():
    """legal_transient should be a small fraction, unknown should be the largest."""
    from collections import Counter
    results = run_pipeline()
    counts = Counter(r["tier"] for r in results)
    assert counts[TIER_LEGAL_TRANSIENT] < counts.get("unknown", 0), \
        f"legal_transient ({counts[TIER_LEGAL_TRANSIENT]}) >= unknown ({counts.get('unknown', 0)}) — suspicious"
