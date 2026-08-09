from __future__ import annotations

import math
import random
from collections import Counter
from typing import Any


def retrieval_metrics(
    rankings: list[list[str]], relevant: list[set[str]], *, k: int = 5
) -> dict[str, float]:
    recalls = []
    reciprocal_ranks = []
    ndcgs = []
    for ranking, gold in zip(rankings, relevant, strict=True):
        top = ranking[:k]
        recalls.append(len(set(top) & gold) / len(gold) if gold else 1.0)
        first = next((index for index, item in enumerate(ranking, start=1) if item in gold), None)
        reciprocal_ranks.append(1.0 / first if first else 0.0)
        dcg = sum(
            1.0 / math.log2(index + 1)
            for index, item in enumerate(ranking[:10], start=1)
            if item in gold
        )
        ideal = sum(1.0 / math.log2(index + 1) for index in range(1, min(10, len(gold)) + 1))
        ndcgs.append(dcg / ideal if ideal else 1.0)
    return {
        f"recall_at_{k}": _mean(recalls),
        "mrr": _mean(reciprocal_ranks),
        "ndcg_at_10": _mean(ndcgs),
    }


def classification_metrics(
    expected: list[str], predicted: list[str], labels: list[str]
) -> dict[str, Any]:
    per_label = {}
    for label in labels:
        true_positive = sum(
            actual == label and guess == label
            for actual, guess in zip(expected, predicted, strict=True)
        )
        false_positive = sum(
            actual != label and guess == label
            for actual, guess in zip(expected, predicted, strict=True)
        )
        false_negative = sum(
            actual == label and guess != label
            for actual, guess in zip(expected, predicted, strict=True)
        )
        precision = (
            true_positive / (true_positive + false_positive)
            if true_positive + false_positive
            else 1.0
        )
        recall = (
            true_positive / (true_positive + false_negative)
            if true_positive + false_negative
            else 1.0
        )
        f1 = 2 * precision * recall / (precision + recall) if precision + recall else 0.0
        per_label[label] = {"precision": precision, "recall": recall, "f1": f1}
    return {
        "precision": _mean([item["precision"] for item in per_label.values()]),
        "recall": _mean([item["recall"] for item in per_label.values()]),
        "f1": _mean([item["f1"] for item in per_label.values()]),
        "accuracy": sum(a == b for a, b in zip(expected, predicted, strict=True)) / len(expected),
        "per_label": per_label,
        "confusion": {
            f"{actual}->{guess}": count
            for (actual, guess), count in Counter(zip(expected, predicted, strict=True)).items()
        },
    }


def bootstrap_mean_difference(
    baseline: list[float], treatment: list[float], *, seed: int, rounds: int = 4000
) -> dict[str, float]:
    if len(baseline) != len(treatment) or not baseline:
        raise ValueError("paired bootstrap requires equally sized non-empty samples")
    rng = random.Random(seed)  # noqa: S311 - reproducible bootstrap, not cryptography
    differences = []
    for _ in range(rounds):
        indices = [rng.randrange(len(baseline)) for _ in baseline]
        differences.append(_mean([treatment[index] - baseline[index] for index in indices]))
    differences.sort()
    lower = differences[int(rounds * 0.025)]
    upper = differences[min(rounds - 1, int(rounds * 0.975))]
    observed = _mean(treatment) - _mean(baseline)
    return {"difference": observed, "ci95_low": lower, "ci95_high": upper}


def percentile(values: list[float], value: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    index = min(len(ordered) - 1, max(0, math.ceil(value * len(ordered)) - 1))
    return ordered[index]


def _mean(values: list[float]) -> float:
    return sum(values) / len(values) if values else 0.0
