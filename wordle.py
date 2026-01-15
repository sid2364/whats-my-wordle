#!/usr/bin/env python3
"""
wordle.py

A Wordle helper that suggests guesses by maximizing expected information gain (entropy).
You play Wordle elsewhere; after each guess you type the feedback pattern here.

Feedback format:
- Use 5 letters of: g (green), y (yellow), b (black/gray)
  Example: "bygyb"
- Or 5 digits: 2 (green), 1 (yellow), 0 (gray)
  Example: "02120"

Word list:
- By default, it tries to load 5-letter words from /usr/share/dict/words (Linux).
- You can pass --words and optionally --answers to use official lists (recommended).

Usage:
  python3 wordle.py
  python3 wordle.py --words official_allowed_guesses.txt --answers shuffled_real_wordles.txt
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import pathlib
import re
import sys
from collections import Counter, defaultdict
from typing import Dict, Iterable, List, Tuple

import tqdm


Pattern = Tuple[int, int, int, int, int]  # each int in {0,1,2}


def parse_pattern(s: str) -> Pattern:
    s = s.strip().lower()
    if re.fullmatch(r"[gyb]{5}", s):
        m = {"b": 0, "y": 1, "g": 2}
        return tuple(m[ch] for ch in s)  # type: ignore
    if re.fullmatch(r"[012]{5}", s):
        return tuple(int(ch) for ch in s)  # type: ignore
    raise ValueError("Pattern must be 5 chars of [g,y,b] or [0,1,2]. Example: 'bygyb' or '02120'.")


def load_words_from_file(path: str) -> List[str]:
    words: List[str] = []
    with open(path, "r", encoding="utf-8", errors="ignore") as f:
        for line in f:
            w = line.strip().lower()
            if len(w) == 5 and w.isalpha():
                words.append(w)
    # Deduplicate while keeping order
    seen = set()
    out = []
    for w in words:
        if w not in seen:
            seen.add(w)
            out.append(w)
    return out


def default_word_source() -> str | None:
    candidates = [
        "/usr/share/dict/words",           # common on Linux
        "/usr/dict/words",
        "/usr/share/dict/web2",            # some distros
    ]
    for p in candidates:
        if os.path.exists(p):
            return p
    return None


def wordle_feedback(secret: str, guess: str) -> Pattern:
    """
    Compute Wordle-style feedback for guess given the secret.
    2 = green, 1 = yellow, 0 = gray.
    Handles repeated letters correctly.
    """
    # First pass: greens
    res = [0] * 5
    secret_counts = Counter(secret)

    for i, (s_ch, g_ch) in enumerate(zip(secret, guess)):
        if g_ch == s_ch:
            res[i] = 2
            secret_counts[g_ch] -= 1

    # Second pass: yellows (only for non-greens)
    for i, g_ch in enumerate(guess):
        if res[i] == 0 and secret_counts[g_ch] > 0:
            res[i] = 1
            secret_counts[g_ch] -= 1

    return tuple(res)  # type: ignore


def entropy_from_counts(counts: Iterable[int], total: int) -> float:
    """Shannon entropy in bits, from bucket counts."""
    if total <= 0:
        return 0.0
    h = 0.0
    for c in counts:
        if c:
            p = c / total
            h -= p * math.log2(p)
    return h


class WordleEntropySolver:
    _FIRST_GUESS_CACHE_FORMAT_VERSION = 1

    def __init__(self, allowed_guesses: List[str], possible_answers: List[str] | None = None):
        self.allowed_guesses = allowed_guesses
        self.possible_answers = possible_answers if possible_answers is not None else allowed_guesses[:]
        self.candidates = self.possible_answers[:]

        # True until the first call to filter_candidates(). Turn 1 scoring is deterministic
        # for a given (allowed_guesses, possible_answers, guess_space), so we can cache it.
        self._is_initial_state = True

        # Store cache next to this script so it is shared across runs.
        script_dir = pathlib.Path(__file__).resolve().parent
        self._first_guess_cache_path = script_dir / ".first_guess_entropy_cache.json"

        # Cache: (guess, tuple(sorted(candidates))?) would be huge. Instead cache per (guess, secret) feedback
        # to speed repeated evaluations across iterations.
        self._fb_cache: Dict[Tuple[str, str], Pattern] = {}

    def feedback(self, secret: str, guess: str) -> Pattern:
        key = (secret, guess)
        if key in self._fb_cache:
            return self._fb_cache[key]
        p = wordle_feedback(secret, guess)
        self._fb_cache[key] = p
        return p

    def filter_candidates(self, guess: str, pattern: Pattern) -> None:
        self._is_initial_state = False
        self.candidates = [w for w in self.candidates if self.feedback(w, guess) == pattern]

    def _hash_word_list(self, words: List[str]) -> str:
        # Joining is cheap compared to entropy scoring; this gives robust invalidation when lists change.
        payload = "\n".join(words).encode("utf-8")
        return hashlib.sha256(payload).hexdigest()

    def _first_guess_cache_key(self, pool: List[str], guess_space: str) -> str:
        pool_hash = self._hash_word_list(pool)
        cand_hash = self._hash_word_list(self.candidates)
        return (
            f"v{self._FIRST_GUESS_CACHE_FORMAT_VERSION}|"
            f"guess_space={guess_space}|pool={pool_hash}|candidates={cand_hash}"
        )

    def _load_first_guess_scoring(self, cache_key: str) -> List[Tuple[str, float]] | None:
        try:
            with open(self._first_guess_cache_path, "r", encoding="utf-8") as f:
                data = json.load(f)
        except FileNotFoundError:
            return None
        except (OSError, json.JSONDecodeError):
            return None

        entry = data.get(cache_key)
        if not isinstance(entry, dict):
            return None
        scored = entry.get("scored")
        if not isinstance(scored, list):
            return None

        out: List[Tuple[str, float]] = []
        for item in scored:
            if (
                isinstance(item, list)
                and len(item) == 2
                and isinstance(item[0], str)
                and isinstance(item[1], (int, float))
            ):
                out.append((item[0], float(item[1])))
        return out or None

    def _save_first_guess_scoring(self, cache_key: str, scored: List[Tuple[str, float]]) -> None:
        try:
            existing: dict = {}
            try:
                with open(self._first_guess_cache_path, "r", encoding="utf-8") as f:
                    existing = json.load(f)
                    if not isinstance(existing, dict):
                        existing = {}
            except FileNotFoundError:
                existing = {}
            except (OSError, json.JSONDecodeError):
                existing = {}

            existing[cache_key] = {
                "format_version": self._FIRST_GUESS_CACHE_FORMAT_VERSION,
                "scored": [[w, h] for (w, h) in scored],
            }

            tmp_path = str(self._first_guess_cache_path) + ".tmp"
            with open(tmp_path, "w", encoding="utf-8") as f:
                json.dump(existing, f, ensure_ascii=False, separators=(",", ":"))
            os.replace(tmp_path, self._first_guess_cache_path)
        except OSError:
            # Cache is an optimization; ignore write failures.
            return

    def score_guess_entropy(self, guess: str) -> float:
        """
        Expected information gain (entropy of feedback distribution) for this guess,
        assuming the secret is uniformly distributed over current candidates.
        """
        buckets: Dict[Pattern, int] = defaultdict(int)
        for secret in self.candidates:
            buckets[self.feedback(secret, guess)] += 1
        return entropy_from_counts(buckets.values(), total=len(self.candidates))

    def suggest(
        self,
        top_k: int = 10,
        guess_space: str = "allowed",
        show_progress: bool = True,
    ) -> List[Tuple[str, float]]:
        """
        Return top_k guesses by entropy.
        guess_space: "allowed" or "candidates"
        """
        pool = self.allowed_guesses if guess_space == "allowed" else self.candidates

        # If candidates are tiny, just return them
        if len(self.candidates) <= 2:
            return [(w, 0.0) for w in self.candidates][:top_k]

        # Fast path: first turn scoring is deterministic for a given word list(s) + guess_space.
        if self._is_initial_state:
            cache_key = self._first_guess_cache_key(pool=pool, guess_space=guess_space)
            cached = self._load_first_guess_scoring(cache_key)
            if cached is not None:
                return cached[:top_k]

        scored: List[Tuple[str, float]] = []
        iterator = tqdm.tqdm(pool, desc="Scoring guesses", unit="word") if show_progress else pool
        for g in iterator:
            h = self.score_guess_entropy(g)
            scored.append((g, h))

        scored.sort(key=lambda x: x[1], reverse=True)

        if self._is_initial_state:
            # Store the full sorted table so subsequent runs can return top_k instantly.
            self._save_first_guess_scoring(
                cache_key=self._first_guess_cache_key(pool=pool, guess_space=guess_space),
                scored=scored,
            )
        return scored[:top_k]


def cli():
    ap = argparse.ArgumentParser(description="Wordle entropy solver (interactive CLI).")
    ap.add_argument("--words", type=str, default=None,
                    help="Path to allowed guess words (5-letter). One per line.")
    ap.add_argument("--answers", type=str, default=None,
                    help="Path to possible answer words (5-letter). One per line. If omitted, uses --words list.")
    ap.add_argument("--top", type=int, default=10, help="How many suggestions to show each turn.")
    ap.add_argument("--guess-space", choices=["allowed", "candidates"], default="allowed",
                    help="Score guesses from all allowed words or only remaining candidates.")
    args = ap.parse_args()

    words_path = args.words
    if words_path is None:
        words_path = default_word_source()
        if words_path is None:
            print("No default word list found. Provide one with --words.", file=sys.stderr)
            sys.exit(1)

    allowed = load_words_from_file(words_path)
    if not allowed:
        print(f"Loaded 0 usable words from {words_path}. Check the file.", file=sys.stderr)
        sys.exit(1)

    if args.answers:
        answers = load_words_from_file(args.answers)
        if not answers:
            print(f"Loaded 0 usable answers from {args.answers}. Check the file.", file=sys.stderr)
            sys.exit(1)
    else:
        answers = None

    solver = WordleEntropySolver(allowed_guesses=allowed, possible_answers=answers)

    print("\n=== Wordle Entropy Solver ===")
    print(f"Allowed guesses: {len(solver.allowed_guesses)}")
    print(f"Possible answers: {len(solver.possible_answers)}")
    print("Feedback input: 5 letters [g,y,b] or digits [2,1,0]. Example: bygyb or 02120")
    print("Type 'quit' to exit.\n")

    turn = 1
    while True:
        n = len(solver.candidates)
        if n == 0:
            print("No candidates left. Either the word list doesn't match the game's dictionary,")
            print("or a feedback pattern was mistyped. (Wordle is petty like that.)")
            break

        print(f"Turn {turn} | Remaining candidates: {n}")
        if n <= 20:
            print("Candidates:", " ".join(solver.candidates))

        suggestions = solver.suggest(top_k=args.top, guess_space=args.guess_space)
        if suggestions:
            best_word, best_h = suggestions[0]
            print("\nTop suggestions (guess | expected bits):")
            for w, h in suggestions:
                print(f"  {w}  |  {h:.4f}")
            print(f"\nSuggested guess: {best_word}\n")
        else:
            best_word = solver.candidates[0]
            print(f"Suggested guess: {best_word}\n")

        guess = input("Enter the guess you used (or press Enter to use suggested): ").strip().lower()
        if guess == "":
            guess = best_word
        if guess == "quit":
            break
        
        # guess is always best_word
        # guess = best_word
        
        if len(guess) != 5 or not guess.isalpha():
            print("Guess must be exactly 5 letters.\n")
            continue

        pat_s = input("Enter the feedback pattern (g/y/b or 2/1/0): ").strip().lower()
        if pat_s == "quit":
            break
        try:
            pattern = parse_pattern(pat_s)
        except ValueError as e:
            print(f"{e}\n")
            continue

        if pattern == (2, 2, 2, 2, 2):
            print(f"\nSolved in {turn} turns. The universe is temporarily lawful.\n")
            break

        solver.filter_candidates(guess, pattern)
        print("")
        turn += 1


if __name__ == "__main__":
    cli()
