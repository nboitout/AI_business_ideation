#!/usr/bin/env python3
"""Run the Claude-subscription stages on any platform (Windows, macOS, Linux).

Every model call is a headless `claude -p` invocation billed to the Claude plan
the CLI is logged in with (see src/llm_backends.py).  Run from anywhere:

  python src/claude_stages.py check           offline syntax checks and unit tests
  python src/claude_stages.py status          progress of a run: saved calls and finished stages (no calls)
  python src/claude_stages.py probe           6 real judgments among three originals
  python src/claude_stages.py rank-originals  Stage 0: rank the 30 originals, derive the 15 seeds
  python src/claude_stages.py local           Tier 1: twelve rounds of local search from the top seed
  python src/claude_stages.py local-score     Tier 1: score the incumbents against the 30 originals

Options: --model (default claude-sonnet-5), --run (the run id, default
claude-sonnet5-v1; keep it fixed across reruns so completed calls replay from
its ledger), --concurrency (default 3), --steps (default 12).  On Windows use
`py` in place of `python`.  The Makefile's claude-* targets call this script.
"""

from __future__ import annotations

import argparse
import os
from pathlib import Path
import subprocess
import sys

import config

ROOT = Path(__file__).resolve().parents[1]
PROBE_FIRMS = ("dreamie", "clockchain", "my-story")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Claude-subscription stages of the conceptual search.")
    parser.add_argument("stage", choices=("check", "status", "probe", "rank-originals", "local", "local-score"))
    parser.add_argument("--model", default="claude-sonnet-5", help="Claude model name passed to `claude --model`.")
    parser.add_argument("--run", default="claude-sonnet5-v1", help="Run id: the prefix of every output file.")
    parser.add_argument("--concurrency", type=int, default=3, help="Calls in flight at once.")
    parser.add_argument("--steps", type=int, default=config.NUM_STEPS, help="Local-search rounds.")
    return parser.parse_args()


# Runs a stage script so that Ctrl+C ends it with one line instead of the
# tracebacks and pipe-cleanup warnings asyncio prints when interrupted.
QUIET_INTERRUPT = """
import runpy, sys
sys.argv = sys.argv[1:]
sys.path.insert(0, "src")
try:
    runpy.run_path(sys.argv[0], run_name="__main__")
except KeyboardInterrupt:
    sys.unraisablehook = lambda unraisable: None
    sys.exit(130)
"""
INTERRUPTED = (
    "Stopped. Every completed call is saved in the run's ledger: rerun the same "
    "command to continue where it stopped."
)


def run(command: list[str], env: dict[str, str] | None = None) -> None:
    child_env = os.environ.copy()
    # UTF-8 for logs, CSVs, and the console on Windows, whose default code page
    # cannot encode characters Claude writes (such as the dash and bullet).
    child_env.update({"PYTHONUTF8": "1", "PYTHONDONTWRITEBYTECODE": "1"})
    child_env.update(env or {})
    print("[run] " + " ".join(command), flush=True)
    if command[0] == sys.executable and command[1].endswith(".py"):
        command = [command[0], "-c", QUIET_INTERRUPT, *command[1:]]
    process = subprocess.Popen(command, cwd=ROOT, env=child_env)
    try:
        returncode = process.wait()
    except KeyboardInterrupt:
        try:
            process.wait(timeout=30)
        except (KeyboardInterrupt, subprocess.TimeoutExpired):
            process.kill()
        raise SystemExit("\n" + INTERRUPTED)
    if returncode == 130:
        raise SystemExit("\n" + INTERRUPTED)
    if returncode != 0:
        raise SystemExit(returncode)


def status(run_id: str) -> None:
    """Report a run's progress from its files, without any model call."""
    import sqlite3

    out = ROOT / "out"
    for label, ledger in (("probe", out / f"{run_id}-probe-call-cache.sqlite3"),
                          ("run", out / f"{run_id}-call-cache.sqlite3")):
        if not ledger.exists():
            print(f"{label} ledger: none yet ({ledger.relative_to(ROOT)})")
            continue
        connection = sqlite3.connect(ledger)
        rows = connection.execute(
            "SELECT role, COUNT(*), SUM(input_tokens), SUM(output_tokens) FROM calls GROUP BY role ORDER BY role"
        ).fetchall()
        last = connection.execute("SELECT MAX(created_at) FROM calls").fetchone()[0]
        connection.close()
        total = sum(row[1] for row in rows)
        print(f"{label} ledger: {total} saved calls, last at {last}")
        for role, count, tokens_in, tokens_out in rows:
            print(f"  {role:22s} {count:5d} calls  {tokens_in or 0:>10,} in  {tokens_out or 0:>9,} out")
    stages = (
        ("rank-originals", f"{run_id}-firm-set-15.txt", "870 judgments"),
        ("local", f"{run_id}-local-search.csv", "about 500 calls"),
        ("local-score", f"{run_id}-universe-common-quality.csv", "up to 780 judgments"),
    )
    print("stages:")
    for name, output, size in stages:
        done = (out / output).exists()
        print(f"  {name:15s} {'done' if done else 'not finished':13s} ({size}; output {output})")
    seed_file = out / f"{run_id}-firm-set-15.txt"
    if seed_file.exists():
        print(f"local-search seed: {local_seed(run_id)}")


def local_seed(run_id: str) -> str:
    seed_file = ROOT / "out" / f"{run_id}-firm-set-15.txt"
    if not seed_file.exists():
        raise SystemExit(f"ERROR: {seed_file.relative_to(ROOT)} not found; run the rank-originals stage first.")
    ids = [line.split("#", 1)[0].strip() for line in seed_file.read_text(encoding="utf-8").splitlines()]
    return next(firm_id for firm_id in ids if firm_id)


def main() -> None:
    args = parse_args()
    python = sys.executable
    if args.stage == "check":
        run([python, "-c", (
            "import ast, pathlib; [ast.parse(p.read_text(encoding='utf-8'), filename=str(p)) "
            "for p in list(pathlib.Path('src').glob('*.py')) + list(pathlib.Path('analysis').glob('*.py'))]"
        )])
        run([python, "-m", "unittest", "discover", "-s", "src/tests", "-v"])
        return

    if args.stage == "status":
        status(args.run)
        return

    model = config.CLAUDE_CLI_PREFIX + args.model
    try:
        config.require_live_backend(model)
    except RuntimeError as exc:
        raise SystemExit(f"ERROR: {exc}")
    if args.concurrency < 1:
        raise SystemExit("ERROR: --concurrency must be at least 1")
    (ROOT / "out").mkdir(exist_ok=True)
    env = {
        "AI_ENTREP_CALL_CACHE": f"out/{args.run}-call-cache.sqlite3",
        "AI_ENTREP_MAX_CONCURRENT_CALLS": str(args.concurrency),
    }

    if args.stage == "probe":
        firms = ROOT / "out" / f"{args.run}-probe-firms.txt"
        firms.write_text("\n".join(PROBE_FIRMS) + "\n", encoding="utf-8")
        env["AI_ENTREP_CALL_CACHE"] = f"out/{args.run}-probe-call-cache.sqlite3"
        run([python, "src/rank_originals.py", "--model", model, "--firm-set", f"out/{args.run}-probe-firms.txt",
             "--seeds", "1", "--run-prefix", f"{args.run}-probe", "--output-dir", "out"], env)
    elif args.stage == "rank-originals":
        run([python, "src/rank_originals.py", "--model", model, "--firm-set", "in/firm-set-30.txt",
             "--run-prefix", args.run, "--output-dir", "out"], env)
    elif args.stage == "local":
        seed = local_seed(args.run)
        run([python, "src/local_search.py", "--seed-id", seed, "--model", model, "--steps", str(args.steps),
             "--input-file", "in/projects.csv", "--run-prefix", args.run, "--output-dir", "out"],
            {**env, "AI_ENTREP_SEED_PROJECT_ID": seed})
    elif args.stage == "local-score":
        seed = local_seed(args.run)
        if not (ROOT / "out" / f"{args.run}-local-search.csv").exists():
            raise SystemExit("ERROR: run the local stage first.")
        run([python, "src/common_quality.py", "--local-csv", f"out/{args.run}-local-search.csv",
             "--firm-set", "in/firm-set-30.txt", "--model", model, "--run-prefix", f"{args.run}-universe",
             "--input-file", "in/projects.csv", "--output-dir", "out", "--scope", "frontier"],
            {**env, "AI_ENTREP_SEED_PROJECT_ID": seed, "AI_ENTREP_EXPECTED_FIRM_COUNT": "30"})


if __name__ == "__main__":
    main()
