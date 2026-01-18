#!/usr/bin/env python3
"""nyt_wordle_bot.py

Automates NYT Wordle in a real browser and drives the solver from wordle.py.

What it does:
- Opens https://www.nytimes.com/games/wordle/index.html
- Dismisses common overlays (intro modal / cookie prompts)
- Enters guesses (keyboard)
- Reads per-tile evaluations (correct/present/absent)
- Feeds feedback into WordleEntropySolver until solved

Notes:
- This relies on Wordle using open shadow DOM for its board components.
- NYT can change the DOM at any time; if selectors break, we update the JS in _read_row_evaluations().

Usage:
  python3 nyt_wordle_bot.py --words official_allowed_guesses.txt --answers shuffled_real_wordles.txt

First run requires Playwright browser install:
  python3 -m playwright install chromium
"""

from __future__ import annotations

import argparse
import re
import sys
import time
from typing import List, Optional, Tuple

import wordle

try:
    from playwright.sync_api import Error as PlaywrightError
    from playwright.sync_api import TimeoutError as PlaywrightTimeoutError
    from playwright.sync_api import sync_playwright
except ModuleNotFoundError:  # pragma: no cover
    PlaywrightError = Exception  # type: ignore
    PlaywrightTimeoutError = TimeoutError  # type: ignore
    sync_playwright = None  # type: ignore


NYT_WORDLE_URL = "https://www.nytimes.com/games/wordle/index.html"


def _pattern_from_evals(evals: List[str]) -> wordle.Pattern:
    # Map Wordle eval strings -> our ints
    m = {
        "correct": 2,
        "present": 1,
        "absent": 0,
    }
    if len(evals) != 5:
        raise ValueError(f"Expected 5 evaluations, got {len(evals)}: {evals}")
    try:
        return tuple(m[e] for e in evals)  # type: ignore
    except KeyError as e:
        raise ValueError(f"Unknown evaluation value: {e!r} in {evals}")


def _dismiss_overlays(page) -> None:
    # Wordle overlays differ by geography/time; keep this best-effort and non-fatal.
    # Consent banners are sometimes inside iframes.

    def _click_first(locator, *, timeout_ms: int = 700) -> None:
        try:
            locator.first.click(timeout=timeout_ms, force=True)
        except (PlaywrightTimeoutError, PlaywrightError):
            return

    # 1) Escape tends to close the "How To Play" modal.
    for _ in range(6):
        try:
            page.keyboard.press("Escape")
        except PlaywrightError:
            pass

    # 2) Try to click common close/continue/consent buttons in the main page and all frames.
    # Prefer "Accept all" to fully dismiss cookie prompts.
    button_patterns = [
        r"accept\s+all",
        r"accept",
        r"agree",
        r"continue",
        r"ok",
        r"got\s+it",
        r"play",
        r"close",
        r"not\s+now",
    ]

    link_patterns = [
        r"accept\s+all",
        r"accept",
        r"agree",
        r"continue",
    ]

    # Targets: main page + any iframes (consent managers often use frames)
    targets = []
    try:
        targets = list(page.frames)
    except PlaywrightError:
        targets = []

    # Ensure the main frame/page is also included.
    targets.append(page)

    for t in targets:
        # Close buttons (often the X in the "How To Play" modal)
        try:
            _click_first(t.locator("button[aria-label='Close'], button[aria-label*='close' i]"))
        except PlaywrightError:
            pass

        # Role-based clicks
        for pat in button_patterns:
            try:
                _click_first(t.get_by_role("button", name=re.compile(pat, re.I)))
            except PlaywrightError:
                pass

        for pat in link_patterns:
            try:
                _click_first(t.get_by_role("link", name=re.compile(pat, re.I)))
            except PlaywrightError:
                pass

    # 3) Some consent UIs are div-based; try a few text locators as a last resort.
    for text_pat in [r"accept\s+all", r"reject\s+all", r"manage\s+preferences"]:
        try:
            _click_first(page.locator(f"text=/{text_pat}/i"))
        except PlaywrightError:
            pass


def _read_row_evaluations(page, row_index: int) -> Optional[List[str]]:
    """Return ['correct'|'present'|'absent']*5 for row_index, or None if not ready."""

    js = r"""
(rowIndex) => {
    const normalize = (v) => {
        const s = String(v || '').toLowerCase();
        if (s === 'correct' || s === 'present' || s === 'absent') return s;
        // While animating or before submitting, tiles are often empty/tbd.
        return null;
    };

    // A) Older implementation: custom elements + shadow DOM
    const app = document.querySelector('game-app');
    if (app && app.shadowRoot) {
        const board = app.shadowRoot.querySelector('game-board');
        if (board && board.shadowRoot) {
            const rows = board.shadowRoot.querySelectorAll('game-row');
            if (rows && rowIndex >= 0 && rowIndex < rows.length) {
                const row = rows[rowIndex];
                if (row && row.shadowRoot) {
                    const tiles = row.shadowRoot.querySelectorAll('game-tile');
                    if (tiles && tiles.length === 5) {
                        const out = [];
                        for (const t of tiles) {
                            const ev = t.getAttribute('evaluation') || t.getAttribute('data-state') || t.getAttribute('data-evaluation');
                            const n = normalize(ev);
                            if (!n) return null;
                            out.push(n);
                        }
                        return out;
                    }
                }
            }
        }
    }

    // B) Newer implementation (as of 2026-01): React DOM under <main id="wordle-app-game">.
    const gameRoot = document.querySelector('#wordle-app-game');
    if (!gameRoot) return null;

    const rows = gameRoot.querySelectorAll('[role="group"][aria-label^="Row"]');
    if (!rows || rowIndex < 0 || rowIndex >= rows.length) return null;

    const row = rows[rowIndex];
    const tiles = row.querySelectorAll('[data-testid="tile"]');
    if (!tiles || tiles.length !== 5) return null;

    const out = [];
    for (const t of tiles) {
        const state = t.getAttribute('data-state');
        const n = normalize(state);
        if (!n) return null;
        out.push(n);
    }
    return out;
}
"""

    try:
        return page.evaluate(js, row_index)
    except PlaywrightError:
        return None


def _wait_for_row_evaluations(page, row_index: int, timeout_s: float) -> List[str]:
    deadline = time.time() + timeout_s
    last = None
    while time.time() < deadline:
        evs = _read_row_evaluations(page, row_index)
        if evs is not None:
            return evs
        last = evs
        time.sleep(0.05)
    raise TimeoutError(f"Timed out waiting for evaluations for row {row_index}. Last={last}")


def _try_submit_guess(page, guess: str, row_index: int, *, eval_timeout_s: float) -> Optional[List[str]]:
    """Type guess+Enter. Return evaluations if accepted; None if it looks rejected."""
    page.keyboard.type(guess)
    page.keyboard.press("Enter")

    # If the guess is rejected ('Not in word list'), the row won't evaluate.
    # We treat a timeout as rejection and clear the row.
    try:
        return _wait_for_row_evaluations(page, row_index, timeout_s=eval_timeout_s)
    except TimeoutError:
        # Clear the row (5 backspaces) and return None.
        for _ in range(5):
            try:
                page.keyboard.press("Backspace")
            except PlaywrightError:
                pass
        return None


def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(description="Automate NYT Wordle and solve it using wordle.py's entropy solver.")
    ap.add_argument("--words", type=str, default="official_allowed_guesses.txt", help="Allowed guess list file.")
    ap.add_argument("--answers", type=str, default="shuffled_real_wordles.txt", help="Possible answers list file.")
    ap.add_argument("--guess-space", choices=["allowed", "candidates"], default="candidates")
    ap.add_argument("--first-guess", type=str, default=None, help="Force a specific first guess.")
    ap.add_argument("--headless", action="store_true", help="Run browser headless.")
    ap.add_argument("--slowmo", type=int, default=0, help="Slow down actions (ms) for debugging.")
    ap.add_argument("--max-turns", type=int, default=6)
    ap.add_argument("--eval-timeout", type=float, default=6.0, help="Seconds to wait for tile evaluations per guess.")
    args = ap.parse_args(argv)

    if sync_playwright is None:
        print(
            "Playwright is not installed.\n\n"
            "Install dependencies:\n"
            "  python3 -m pip install -r requirements.txt\n\n"
            "Then install the browser binary (one-time):\n"
            "  python3 -m playwright install chromium\n",
            file=sys.stderr,
        )
        return 2

    allowed = wordle.load_words_from_file(args.words)
    if not allowed:
        print(f"Loaded 0 allowed guesses from {args.words}", file=sys.stderr)
        return 2

    answers = wordle.load_words_from_file(args.answers)
    if not answers:
        print(f"Loaded 0 possible answers from {args.answers}", file=sys.stderr)
        return 2

    if args.first_guess is not None and args.first_guess not in allowed:
        print(f"Forced first guess '{args.first_guess}' is not in the allowed guess list.", file=sys.stderr)
        return 2

    solver = wordle.WordleEntropySolver(allowed_guesses=allowed, possible_answers=answers)

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=args.headless, slow_mo=args.slowmo)
        context = browser.new_context(viewport={"width": 1100, "height": 900})
        page = context.new_page()

        page.goto(NYT_WORDLE_URL, wait_until="domcontentloaded")
        page.wait_for_timeout(1000)
        _dismiss_overlays(page)

        # Ensure the game is present (shadow DOM ready).
        try:
            page.wait_for_selector("#wordle-app-game, game-app, wordle-app-game", timeout=15000)
        except PlaywrightTimeoutError:
            print("Could not find the Wordle game element; page structure may have changed.", file=sys.stderr)
            return 3

        # Click into the page so keystrokes go to the game.
        try:
            page.mouse.click(50, 50)
        except PlaywrightError:
            pass

        first_guess_used: Optional[str] = args.first_guess

        for turn in range(1, args.max_turns + 1):
            suggestions = solver.suggest(
                top_k=20,
                guess_space=args.guess_space,
                show_progress=False,
                force_first_guess=first_guess_used if turn == 1 else None,
            )
            if not suggestions:
                print("No suggestions left; solver has no candidates.")
                return 4

            # Try suggestions until one is accepted by NYT.
            evaluations: Optional[List[str]] = None
            guess_used: Optional[str] = None
            for (guess, _h) in suggestions:
                # Dismiss overlays between turns; sometimes share/login popups appear mid-game.
                _dismiss_overlays(page)
                evs = _try_submit_guess(page, guess, row_index=turn - 1, eval_timeout_s=args.eval_timeout)
                if evs is not None:
                    evaluations = evs
                    guess_used = guess
                    break

            if evaluations is None or guess_used is None:
                print("All candidate suggestions were rejected by the site (word list mismatch?).")
                return 5

            if turn == 1 and first_guess_used is None:
                first_guess_used = guess_used

            pattern = _pattern_from_evals(evaluations)
            print(f"Turn {turn}: guess={guess_used} eval={''.join(e[0] for e in evaluations)} pattern={pattern}")

            if pattern == (2, 2, 2, 2, 2):
                print(f"Solved: {guess_used} in {turn} turns")
                return 0

            solver.filter_candidates(guess_used, pattern)
            if not solver.candidates:
                print("No candidates remain after filtering; extraction may be wrong or word lists mismatch.")
                return 6

        print(f"Failed to solve within {args.max_turns} turns. Remaining candidates: {len(solver.candidates)}")
        return 7


if __name__ == "__main__":
    raise SystemExit(main())
