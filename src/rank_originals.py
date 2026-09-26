#!/usr/bin/env python3
"""Rank the reference set under the evaluator's own judgment and derive the seeds.

The paper's seeds are the bottom half of the 30 original ventures under the
evaluator's own double round robin (analysis/evalrank-30.csv, ties at the
boundary broken head-to-head).  A run with a different evaluator model must
re-derive them.  This stage runs that double round robin (30 x 29 = 870
judgments), then writes

  <run-prefix>-evalrank-30.csv       rank, id, win rate, and tie-break notes
  <run-prefix>-evalrank-matches.csv  every judgment with the evaluator's analysis
  <run-prefix>-firm-set-15.txt       the seed set, highest-ranked seed first

The first id in the seed file is the local-search seed, as in
in/firm-set-15-evalrank.txt.
"""

from __future__ import annotations

import argparse
import asyncio
from collections import defaultdict
import csv
import logging
import os
import time

import config
from agents_common import Evaluator, get_cache_stats, get_call_stats, get_error_count, get_usage_stats
from global_search import append_match_records, load_seed_plans
from records import MATCH_FIELDNAMES, initialize_csv

LOG_FORMAT = "%(asctime)s %(message)s"
logging.Formatter.default_msec_format = "%s.%03d"
logging.basicConfig(level=logging.INFO, format=LOG_FORMAT)
logging.getLogger("httpx").setLevel(logging.WARNING)
logger = logging.getLogger(__name__)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Rank the 30 originals and derive the seed set.")
    parser.add_argument("--input-file", default=config.INPUT_FILE)
    parser.add_argument("--firm-set", default="in/firm-set-30.txt", help="The reference set, one id per line.")
    parser.add_argument("--model", required=True)
    parser.add_argument("--seeds", type=int, default=15, help="Size of the bottom-half seed set.")
    parser.add_argument("--run-prefix", required=True)
    parser.add_argument("--output-dir", default="out")
    return parser.parse_args()


def read_firm_set(path: str) -> list[str]:
    with open(path, encoding="utf-8") as f:
        ids = [line.split("#", 1)[0].strip() for line in f]
    return [firm_id for firm_id in ids if firm_id]


def head_to_head(group: list[int], match_details: dict) -> dict[int, float]:
    """Wins of each group member in the matches among the group's members."""
    members = set(group)
    wins: dict[int, float] = defaultdict(float)
    for (i, j), (winner_label, _analysis) in match_details.items():
        if i in members and j in members:
            if winner_label == f"plan_{i}":
                wins[i] += 1
            elif winner_label == f"plan_{j}":
                wins[j] += 1
            else:
                wins[i] += 0.5
                wins[j] += 0.5
    return {k: wins[k] for k in group}


def order_with_tie_breaks(
    ids: list[str],
    win_rates: list[float],
    match_details: dict,
    boundary: int,
) -> tuple[list[int], dict[int, str], bool]:
    """Order by win rate; break equal win rates head-to-head, then by id.

    Returns the order, a note per index, and whether a tie across the seed
    boundary (between rank `boundary` and `boundary + 1`) remained unresolved.
    """
    groups: dict[float, list[int]] = defaultdict(list)
    for idx, rate in enumerate(win_rates):
        groups[round(rate, 9)].append(idx)

    order: list[int] = []
    notes: dict[int, str] = {}
    unresolved_boundary = False
    for rate in sorted(groups, reverse=True):
        group = groups[rate]
        start = len(order)
        if len(group) > 1:
            h2h = head_to_head(group, match_details)
            group = sorted(group, key=lambda k: (-h2h[k], ids[k]))
            crosses = start < boundary < start + len(group)
            summary = ", ".join(f"{ids[k]} {h2h[k]:g}" for k in group)
            note = f"tied at {rate:.4f}; head-to-head wins among the tied: {summary}"
            if crosses and h2h[group[boundary - start - 1]] == h2h[group[boundary - start]]:
                unresolved_boundary = True
                note += "; UNRESOLVED at the seed boundary, broken by id"
            for k in group:
                notes[k] = note
        order.extend(group)
    return order, notes, unresolved_boundary


async def main() -> None:
    args = parse_args()
    config.set_all_agent_models(args.model)
    if not config.MOCK_LLM:
        config.require_live_backend(args.model)

    t0 = time.time()
    os.makedirs(args.output_dir, exist_ok=True)
    prefix = os.path.join(args.output_dir, args.run_prefix)
    # Appended, so that the log of an interrupted and resumed run stays whole.
    handler = logging.FileHandler(f"{prefix}-evalrank.log", mode="a")
    handler.setFormatter(logging.Formatter(LOG_FORMAT))
    logging.getLogger().addHandler(handler)

    firm_ids = read_firm_set(args.firm_set)
    originals = load_seed_plans(args.input_file, len(firm_ids), firm_ids)
    ids = [firm_id for firm_id, _ in originals]
    plans = [plan for _, plan in originals]
    n = len(plans)
    if not 0 < args.seeds < n:
        raise SystemExit(f"--seeds must be between 1 and {n - 1}")
    boundary = n - args.seeds  # the last rank that stays out of the seed set

    logger.info("Reference-set ranking: %d originals, %d judgments, model %s", n, n * (n - 1), args.model)
    ranks, win_rates, match_details = await Evaluator().evaluate(plans)

    match_csv = f"{prefix}-evalrank-matches.csv"
    initialize_csv(match_csv, MATCH_FIELDNAMES)
    append_match_records(match_csv, "evalrank", 0, ids, match_details)

    order, notes, unresolved = order_with_tie_breaks(ids, win_rates, match_details, boundary)
    rank_csv = f"{prefix}-evalrank-30.csv"
    with open(rank_csv, "w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        writer.writerow(["rank", "id", "universe_win_rate", "note"])
        for position, idx in enumerate(order, 1):
            writer.writerow([position, ids[idx], f"{win_rates[idx]:.4f}", notes.get(idx, "")])

    seeds = [ids[idx] for idx in order[boundary:]]
    seed_file = f"{prefix}-firm-set-{args.seeds}.txt"
    with open(seed_file, "w", encoding="utf-8") as f:
        f.write(
            f"# Bottom {args.seeds} of the {n}-venture reference set, ranked by the evaluator's\n"
            f"# own all-{n} double round robin ({os.path.basename(rank_csv)}; model {args.model}).\n"
            "# Highest-ranked seed first; it is the local-search seed.\n"
        )
        f.write("\n".join(seeds) + "\n")

    logger.info("Top 5: %s", ", ".join(f"{ids[i]} {win_rates[i]:.0%}" for i in order[:5]))
    logger.info("Seeds (bottom %d): %s", args.seeds, ", ".join(seeds))
    logger.info("Local-search seed: %s", seeds[0])
    if unresolved:
        logger.warning("A tie across the seed boundary survived the head-to-head; see %s", rank_csv)
    logger.info("Records: %s, %s, %s", rank_csv, match_csv, seed_file)
    logger.info(
        "Calls: %s | cache hits: %s | errors: %d | usage: %s",
        get_call_stats(), get_cache_stats(), get_error_count(), get_usage_stats(),
    )
    logger.info("Elapsed: %.1f min", (time.time() - t0) / 60)
    if get_error_count():
        logger.warning(
            "%d judgments failed and were scored as ties, as in the paper's pipeline; "
            "see the log before using the seed set.", get_error_count()
        )


if __name__ == "__main__":
    asyncio.run(main())
