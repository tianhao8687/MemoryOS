from __future__ import annotations


def reciprocal_rank_fusion(
    rankings: dict[str, list[str]],
    *,
    weights: dict[str, float] | None = None,
    k: int = 60,
) -> tuple[dict[str, float], dict[str, dict[str, int]]]:
    configured = weights or {}
    scores: dict[str, float] = {}
    traces: dict[str, dict[str, int]] = {}
    for channel, ids in rankings.items():
        weight = configured.get(channel, 1.0)
        for rank, item_id in enumerate(ids, start=1):
            scores[item_id] = scores.get(item_id, 0.0) + weight / (k + rank)
            traces.setdefault(item_id, {})[channel] = rank
    return scores, traces
