from __future__ import annotations

import re
from typing import Any

TOKEN = re.compile(r"[\w.]+", re.UNICODE)


def _tokens(value: str) -> set[str]:
    return set(TOKEN.findall(value.lower()))


def _similarity(left: set[str], right: set[str]) -> float:
    union = left | right
    return len(left & right) / len(union) if union else 0.0


def mmr_select(
    candidates: list[dict[str, Any]],
    *,
    limit: int,
    relevance_key: str = "fused_score",
    lambda_relevance: float = 0.78,
) -> list[dict[str, Any]]:
    remaining = list(candidates)
    token_sets = {
        str(item["memory"]["id"]): _tokens(
            f"{item['memory'].get('title', '')} {item['memory'].get('content', '')}"
        )
        for item in remaining
    }
    # Cache each candidate's maximum similarity to the selected set.  Recomputing
    # that maximum from scratch on every round is O(candidates * limit^2) and made
    # a 10k-record context request spend seconds in an otherwise fast local query.
    # The cached value is mathematically identical and updates once per selection.
    max_redundancy = {identity: 0.0 for identity in token_sets}
    selected: list[dict[str, Any]] = []
    while remaining and len(selected) < limit:

        def score(item: dict[str, Any]) -> float:
            identity = str(item["memory"]["id"])
            return (
                lambda_relevance * float(item[relevance_key])
                - (1 - lambda_relevance) * max_redundancy[identity]
            )

        chosen = max(remaining, key=score)
        chosen["mmr_score"] = round(score(chosen), 8)
        selected.append(chosen)
        remaining.remove(chosen)
        chosen_tokens = token_sets[str(chosen["memory"]["id"])]
        for item in remaining:
            identity = str(item["memory"]["id"])
            max_redundancy[identity] = max(
                max_redundancy[identity],
                _similarity(token_sets[identity], chosen_tokens),
            )
    return selected
