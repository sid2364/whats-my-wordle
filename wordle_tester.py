#!/usr/bin/env python3
"""wordle_tester.py

Runs automated simulations using the solver in wordle.py and prints summary statistics.
Optionally writes a matplotlib graph to disk.

Examples:
  python3 wordle_tester.py --words official_allowed_guesses.txt --answers shuffled_real_wordles.txt --limit 200
  python3 wordle_tester.py --guess-space candidates --max-turns 6 --plot results.png

Notes:
- If you don't have dependencies installed yet, you can still run without graphs.
- Use --plot to require matplotlib.
"""

from __future__ import annotations

import argparse
import statistics
import sys
from collections import Counter
from dataclasses import dataclass
from typing import Iterable, List, Optional

import wordle

try:
    import tqdm  # type: ignore
except ModuleNotFoundError:  # pragma: no cover
    tqdm = None  # type: ignore


@dataclass(frozen=True)
class GameResult:
    secret: str
    solved: bool
    turns: int
    final_candidates: int
    first_guess: str


def _default_path_if_exists(path: str) -> Optional[str]:
    try:
        with open(path, "r", encoding="utf-8"):
            return path
    except OSError:
        return None


def _load_default_allowed() -> List[str]:
    preferred = _default_path_if_exists("official_allowed_guesses.txt")
    if preferred:
        return wordle.load_words_from_file(preferred)

    src = wordle.default_word_source()
    if not src:
        raise SystemExit("No default word list found. Provide --words.")
    return wordle.load_words_from_file(src)


def _load_default_answers() -> Optional[List[str]]:
    preferred = _default_path_if_exists("shuffled_real_wordles.txt")
    if preferred:
        return wordle.load_words_from_file(preferred)
    return None


def _iter_progress(iterable, *, enabled: bool, desc: str, unit: str):
    if enabled and tqdm is not None:
        return tqdm.tqdm(iterable, desc=desc, unit=unit)
    return iterable


def simulate_game(
    *,
    secret: str,
    allowed_guesses: List[str],
    possible_answers: Optional[List[str]],
    guess_space: str,
    max_turns: int,
) -> GameResult:
    solver = wordle.WordleEntropySolver(allowed_guesses=allowed_guesses, possible_answers=possible_answers)

    first_guess = ""
    for turn in range(1, max_turns + 1):
        suggestions = solver.suggest(top_k=1, guess_space=guess_space, show_progress=False)
        if not suggestions:
            return GameResult(
                secret=secret,
                solved=False,
                turns=turn,
                final_candidates=len(solver.candidates),
                first_guess=first_guess,
            )

        guess = suggestions[0][0]
        if turn == 1:
            first_guess = guess

        pattern = wordle.wordle_feedback(secret, guess)
        if pattern == (2, 2, 2, 2, 2):
            return GameResult(
                secret=secret,
                solved=True,
                turns=turn,
                final_candidates=len(solver.candidates),
                first_guess=first_guess,
            )

        solver.filter_candidates(guess, pattern)
        if not solver.candidates:
            return GameResult(
                secret=secret,
                solved=False,
                turns=turn,
                final_candidates=0,
                first_guess=first_guess,
            )

    return GameResult(
        secret=secret,
        solved=False,
        turns=max_turns,
        final_candidates=len(solver.candidates),
        first_guess=first_guess,
    )


def summarize(results: Iterable[GameResult]) -> str:
    results = list(results)
    if not results:
        return "No results."

    solved = [r for r in results if r.solved]
    failed = [r for r in results if not r.solved]

    dist = Counter(r.turns for r in solved)
    first_guess_counts = Counter(r.first_guess for r in results if r.first_guess)

    lines: List[str] = []
    lines.append(f"Games: {len(results)}")
    lines.append(f"Solved: {len(solved)} ({len(solved) / len(results) * 100:.2f}%)")
    lines.append(f"Failed: {len(failed)} ({len(failed) / len(results) * 100:.2f}%)")

    if solved:
        turns_list = [r.turns for r in solved]
        lines.append(f"Avg turns (solved): {statistics.mean(turns_list):.3f}")
        lines.append(f"Median turns (solved): {statistics.median(turns_list):.1f}")
        lines.append("Turn distribution (solved): " + ", ".join(f"{t}:{dist[t]}" for t in sorted(dist)))

    if first_guess_counts:
        (top_guess, top_count) = first_guess_counts.most_common(1)[0]
        lines.append(f"Most common first guess: {top_guess} ({top_count} / {len(results)})")

    if failed:
        examples = ", ".join(r.secret for r in failed[:10])
        lines.append(f"Failed examples (up to 10): {examples}")

    return "\n".join(lines)


def plot_results(*, results: List[GameResult], max_turns: int, out_path: str) -> None:
    # Import matplotlib only if plotting is requested.
    import matplotlib

    matplotlib.use("Agg")
    import matplotlib.pyplot as plt  # noqa: E402

    total = len(results)
    solved = [r for r in results if r.solved]
    failed = [r for r in results if not r.solved]

    solved_counts = Counter(r.turns for r in solved)

    xs = list(range(1, max_turns + 1))
    ys = [solved_counts.get(t, 0) for t in xs]

    fail_x = max_turns + 1
    fail_y = len(failed)

    fig, ax = plt.subplots(figsize=(10, 4.5))
    ax.bar(xs, ys, label="Solved", color="C0")
    ax.bar([fail_x], [fail_y], label="Failed", color="C3")

    ax.set_title("Wordle solver results")
    ax.set_xlabel("Turns to solve")
    ax.set_ylabel("# games")
    ax.set_xticks(xs + [fail_x])
    ax.set_xticklabels([str(t) for t in xs] + ["fail"])

    solved_pct = (len(solved) / total * 100.0) if total else 0.0
    ax.text(
        0.99,
        0.95,
        f"Solved: {len(solved)}/{total} ({solved_pct:.1f}%)",
        transform=ax.transAxes,
        ha="right",
        va="top",
    )

    ax.legend(loc="upper left")
    fig.tight_layout()
    fig.savefig(out_path, dpi=150)
    plt.close(fig)


def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(description="Run Wordle solver simulations and print statistics.")
    ap.add_argument("--words", type=str, default=None, help="Allowed guess list (5-letter words).")
    ap.add_argument("--answers", type=str, default=None, help="Possible answers list (5-letter words).")
    ap.add_argument(
        "--secrets",
        type=str,
        default=None,
        help="Secrets to test (defaults to --answers if provided, else shuffled_real_wordles.txt if present).",
    )
    ap.add_argument("--limit", type=int, default=0, help="Limit number of secrets (0 = no limit).")
    ap.add_argument("--max-turns", type=int, default=6, help="Max turns per game.")
    ap.add_argument(
        "--guess-space",
        choices=["allowed", "candidates"],
        default="candidates",
        help="Score guesses from all allowed words or only remaining candidates.",
    )
    ap.add_argument("--no-progress", action="store_true", help="Disable progress bars.")
    ap.add_argument("--plot", type=str, default=None, help="Write a matplotlib graph to this path (e.g. results.png).")
    args = ap.parse_args(argv)

    if args.words:
        allowed = wordle.load_words_from_file(args.words)
    else:
        allowed = _load_default_allowed()

    if not allowed:
        print("Loaded 0 allowed guesses.", file=sys.stderr)
        return 2

    possible_answers: Optional[List[str]]
    if args.answers:
        possible_answers = wordle.load_words_from_file(args.answers)
    else:
        possible_answers = _load_default_answers()

    if args.secrets:
        secrets = wordle.load_words_from_file(args.secrets)
    elif args.answers:
        secrets = wordle.load_words_from_file(args.answers)
    elif possible_answers is not None:
        secrets = possible_answers
    else:
        print("No secrets list available. Provide --answers or --secrets.", file=sys.stderr)
        return 2

    if args.limit and args.limit > 0:
        secrets = secrets[: args.limit]

    possible_set = set(possible_answers) if possible_answers is not None else None
    skipped = 0
    results: List[GameResult] = []

    for secret in _iter_progress(secrets, enabled=(not args.no_progress), desc="Simulating", unit="game"):
        if possible_set is not None and secret not in possible_set:
            skipped += 1
            continue
        results.append(
            simulate_game(
                secret=secret,
                allowed_guesses=allowed,
                possible_answers=possible_answers,
                guess_space=args.guess_space,
                max_turns=args.max_turns,
            )
        )

    if skipped:
        print(f"Skipped {skipped} secrets not in possible answers.")

    print(summarize(results))

    if args.plot:
        try:
            plot_results(results=results, max_turns=args.max_turns, out_path=args.plot)
            print(f"Wrote plot: {args.plot}")
        except ModuleNotFoundError as e:
            print(f"Plot requested but missing dependency: {e}. Install matplotlib to use --plot.", file=sys.stderr)
            return 2

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
