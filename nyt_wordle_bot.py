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
import json
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path
from typing import Callable, List, Optional

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

LogFn = Callable[[str], None]


def _expand_path(p: str) -> Path:
    return Path(p).expanduser().resolve()


def _write_json(path: Path, payload: dict, *, log: Optional[LogFn] = None) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    tmp.replace(path)
    if log is not None:
        log(f"result: wrote {path}")


def _try_notify_send(message: str, *, log: Optional[LogFn] = None, log_debug: Optional[LogFn] = None) -> None:
    """Best-effort desktop notification via notify-send (Linux)."""

    def _dbg(msg: str) -> None:
        if log_debug is not None:
            log_debug(msg)

    if shutil.which("notify-send") is None:
        _dbg("notify: notify-send not found; skipping")
        return

    try:
        subprocess.run(
            ["notify-send", "Wordle bot", message],
            check=False,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        if log is not None:
            log("notify: sent desktop notification")
    except Exception as e:
        _dbg(f"notify: failed: {e}")


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


def _dismiss_overlays(
    page,
    *,
    log_debug: Optional[LogFn] = None,
    aggressive: bool = False,
) -> None:
    # Wordle overlays differ by geography/time; keep this best-effort and non-fatal.
    # Consent banners are sometimes inside iframes.

    def _dbg(msg: str) -> None:
        if log_debug is not None:
            log_debug(msg)

    # Keep click timeouts tiny; this runs a lot.
    click_timeout_ms = 900 if aggressive else 120

    def _click_first(locator, *, what: str, timeout_ms: Optional[int] = None) -> bool:
        try:
            locator.first.click(timeout=(timeout_ms if timeout_ms is not None else click_timeout_ms), force=True)
            _dbg(f"overlay: clicked {what}")
            return True
        except (PlaywrightTimeoutError, PlaywrightError):
            return False

    def _any_overlay_likely_visible() -> bool:
        """Cheap check to avoid spending time scanning frames when nothing is up."""
        # These checks run with very short timeouts.
        try:
            if page.locator("text=/manage privacy preferences/i").first.is_visible(timeout=80):
                return True
        except PlaywrightError:
            pass

        for pat in [r"accept\s+all", r"cookies?", r"how\s+to\s+play", r"subscribe", r"privacy"]:
            try:
                if page.locator(f"text=/{pat}/i").first.is_visible(timeout=80):
                    return True
            except PlaywrightError:
                pass

        # If we can't tell, don't assume overlay is visible.
        return False

    # If we're not being aggressive and nothing obvious is visible, bail quickly.
    if not aggressive and not _any_overlay_likely_visible():
        return

    # 1) Escape tends to close the "How To Play" modal.
    for _ in range(2 if not aggressive else 6):
        try:
            page.keyboard.press("Escape")
            _dbg("overlay: pressed Escape")
        except PlaywrightError:
            pass

    # 2) Cookie consent: keep it simple.
    # User asked to "just accept all cookies", so do ONLY that (plus close buttons).
    # We intentionally avoid clicking "Manage preferences" or "Reject" flows.
    consent_button_patterns = [
        r"accept\s+all(\s+cookies)?",
        r"accept\s+all",
        r"accept",
        r"agree",
    ]

    # Targets: always main page; if aggressive, also relevant iframes.
    targets = [page]
    if aggressive:
        try:
            frames = list(page.frames)
        except PlaywrightError:
            frames = []

        # Consent managers often live in special frames; scanning every ad/analytics frame is slow.
        frame_allowlist = (
            "consent",
            "privacy",
            "cmp",
            "onetrust",
            "quantcast",
            "trustarc",
            "cookielaw",
            "didomi",
        )
        for fr in frames:
            try:
                url = fr.url or ""
            except Exception:
                url = ""
            if any(k in url.lower() for k in frame_allowlist):
                targets.append(fr)

    for t in targets:
        # For debugging, identify which frame we are targeting.
        try:
            frame_url = getattr(t, "url", "")
        except Exception:
            frame_url = ""
        if frame_url:
            _dbg(f"overlay: scanning frame {frame_url}")

        # Close buttons (often the X in the "How To Play" modal)
        try:
            _click_first(
                t.locator("button[aria-label='Close'], button[aria-label*='close' i]"),
                what="close button (aria-label)",
            )
        except PlaywrightError:
            pass

        # Role-based cookie acceptance
        for pat in consent_button_patterns:
            try:
                if _click_first(t.get_by_role("button", name=re.compile(pat, re.I)), what=f"button /{pat}/i"):
                    # If we accepted, no need to try other patterns in this target.
                    break
            except PlaywrightError:
                pass

    # 3) Some consent UIs are div-based; last resort: click visible text "Accept all".
    for text_pat in [r"accept\s+all(\s+cookies)?", r"accept\s+all"]:
        try:
            _click_first(page.locator(f"text=/{text_pat}/i"), what=f"text=/{text_pat}/i")
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
    last_debug_t = 0.0
    while time.time() < deadline:
        evs = _read_row_evaluations(page, row_index)
        if evs is not None:
            return evs
        last = evs
        # Throttle debug output (if enabled by caller via Playwright slowmo).
        now = time.time()
        if now - last_debug_t >= 1.0:
            last_debug_t = now
        time.sleep(0.05)
    raise TimeoutError(f"Timed out waiting for evaluations for row {row_index}. Last={last}")


def _try_submit_guess(
    page,
    guess: str,
    row_index: int,
    *,
    eval_timeout_s: float,
    log: Optional[LogFn] = None,
    log_debug: Optional[LogFn] = None,
) -> Optional[List[str]]:
    """Type guess+Enter. Return evaluations if accepted; None if it looks rejected."""
    if log is not None:
        log(f"browser: typing guess '{guess}' (row {row_index + 1})")
    page.keyboard.type(guess)
    page.keyboard.press("Enter")
    if log_debug is not None:
        log_debug("browser: pressed Enter")

    # If the guess is rejected ('Not in word list'), the row won't evaluate.
    # We treat a timeout as rejection and clear the row.
    try:
        evs = _wait_for_row_evaluations(page, row_index, timeout_s=eval_timeout_s)
        if log is not None:
            log(f"browser: got evaluations for row {row_index + 1}: {evs}")
        return evs
    except TimeoutError:
        if log is not None:
            log(f"browser: guess '{guess}' appears rejected (no evaluation within {eval_timeout_s:.1f}s)")
        # Clear the row (5 backspaces) and return None.
        for _ in range(5):
            try:
                page.keyboard.press("Backspace")
            except PlaywrightError:
                pass
        return None


def _detect_dom_mode(page) -> str:
    """Best-effort detection of which Wordle DOM variant is present."""
    js = r"""
() => {
  const hasReact = !!document.querySelector('#wordle-app-game');
  const hasShadow = !!document.querySelector('game-app');
  if (hasReact && hasShadow) return 'react+shadow';
  if (hasReact) return 'react';
  if (hasShadow) return 'shadow';
  return 'unknown';
}
"""
    try:
        return str(page.evaluate(js))
    except Exception:
        return "unknown"


def _click_play_if_present(page, *, log: Optional[LogFn] = None, log_debug: Optional[LogFn] = None) -> bool:
    """Click the "Play" button on the landing screen if it exists.

    Returns True if we clicked something, else False.
    """

    def _dbg(msg: str) -> None:
        if log_debug is not None:
            log_debug(msg)

    def _info(msg: str) -> None:
        if log is not None:
            log(msg)

    # The landing page has buttons like "Subscribe", "Log in", "Play".
    # Keep timeouts short; this may run multiple times.
    try:
        btn = page.get_by_role("button", name=re.compile(r"^play$", re.I)).first
        if btn.is_visible(timeout=200):
            btn.click(timeout=1200, force=True)
            _info("browser: clicked Play")
            return True
    except (PlaywrightTimeoutError, PlaywrightError) as e:
        _dbg(f"play: role-button click failed: {e}")

    # Fallback: any element containing text "Play" that is clickable.
    try:
        loc = page.locator("text=/^play$/i").first
        if loc.is_visible(timeout=200):
            loc.click(timeout=1200, force=True)
            _info("browser: clicked Play (text fallback)")
            return True
    except (PlaywrightTimeoutError, PlaywrightError) as e:
        _dbg(f"play: text click failed: {e}")

    return False


def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(description="Automate NYT Wordle and solve it using wordle.py's entropy solver.")
    ap.add_argument("--words", type=str, default="official_allowed_guesses.txt", help="Allowed guess list file.")
    ap.add_argument("--answers", type=str, default="shuffled_real_wordles.txt", help="Possible answers list file.")
    ap.add_argument("--guess-space", choices=["allowed", "candidates"], default="candidates")
    ap.add_argument("--first-guess", type=str, default=None, help="Force a specific first guess.")
    ap.add_argument("--headless", action="store_true", help="Run browser headless.")
    ap.add_argument("--slowmo", type=int, default=0, help="Slow down actions (ms) for debugging.")
    ap.add_argument("--verbose", action="store_true", help="Print detailed progress to the console.")
    ap.add_argument("--debug", action="store_true", help="Very verbose logs (overlay clicks, browser console).")
    ap.add_argument(
        "--result-path",
        type=str,
        default=None,
        help="Write the final result to this JSON file (e.g. ~/.cache/wordle-bot/last.json).",
    )
    ap.add_argument(
        "--notify",
        action="store_true",
        help="Send a desktop notification with the answer (best-effort; requires notify-send).",
    )
    ap.add_argument("--max-turns", type=int, default=6)
    ap.add_argument("--eval-timeout", type=float, default=6.0, help="Seconds to wait for tile evaluations per guess.")
    args = ap.parse_args(argv)

    verbose = bool(args.verbose or args.debug)
    debug = bool(args.debug)

    start_t = time.time()

    def log(msg: str) -> None:
        if not verbose:
            return
        dt = time.time() - start_t
        print(f"[{dt:7.2f}s] {msg}")

    def log_debug(msg: str) -> None:
        if not debug:
            return
        dt = time.time() - start_t
        print(f"[{dt:7.2f}s] DEBUG {msg}")

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

    log(f"solver: loaded allowed={len(allowed)} answers={len(answers)} guess_space={args.guess_space}")
    if args.first_guess:
        log(f"solver: forced first guess = {args.first_guess}")

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=args.headless, slow_mo=args.slowmo)
        context = browser.new_context(viewport={"width": 1100, "height": 900})
        page = context.new_page()

        if debug:
            # Pipe browser console + page errors into Python stdout.
            page.on("console", lambda m: log_debug(f"console.{m.type}: {m.text}"))
            page.on("pageerror", lambda e: log_debug(f"pageerror: {e}"))
            page.on("requestfailed", lambda r: log_debug(f"requestfailed: {r.url} {r.failure}"))

        log(f"browser: launching chromium headless={args.headless} slowmo={args.slowmo}ms")
        log(f"browser: navigating to {NYT_WORDLE_URL}")

        page.goto(NYT_WORDLE_URL, wait_until="domcontentloaded")
        page.wait_for_timeout(800)
        # Do an aggressive overlay sweep once at startup.
        _dismiss_overlays(page, log_debug=log_debug if debug else None, aggressive=True)

        # Some regions show a landing screen that requires pressing "Play" before the game loads.
        _click_play_if_present(page, log=log if verbose else None, log_debug=log_debug if debug else None)

        # Ensure the game is present (board UI ready).
        try:
            page.wait_for_selector("#wordle-app-game, game-app, wordle-app-game", timeout=15000)
        except PlaywrightTimeoutError:
            # One more attempt: overlays/cookies can block the Play click.
            _dismiss_overlays(page, log_debug=log_debug if debug else None, aggressive=True)
            _click_play_if_present(page, log=log if verbose else None, log_debug=log_debug if debug else None)
            try:
                page.wait_for_selector("#wordle-app-game, game-app, wordle-app-game", timeout=15000)
            except PlaywrightTimeoutError:
                print("Could not find the Wordle game element; page structure may have changed.", file=sys.stderr)
                return 3

        mode = _detect_dom_mode(page)
        log(f"browser: detected Wordle DOM mode = {mode}")

        # Click into the page so keystrokes go to the game.
        try:
            page.mouse.click(50, 50)
            log("browser: focused game (clicked page)")
        except PlaywrightError:
            pass

        first_guess_used: Optional[str] = args.first_guess

        turns: List[dict] = []

        for turn in range(1, args.max_turns + 1):
            log(f"turn {turn}: candidates remaining = {len(solver.candidates)}")
            suggestions = solver.suggest(
                top_k=20,
                guess_space=args.guess_space,
                show_progress=False,
                force_first_guess=first_guess_used if turn == 1 else None,
            )
            if not suggestions:
                print("No suggestions left; solver has no candidates.")
                return 4

            if verbose:
                preview = ", ".join(w for (w, _h) in suggestions[:8])
                log(f"solver: top suggestions = {preview}{' ...' if len(suggestions) > 8 else ''}")

            # Try suggestions until one is accepted by NYT.
            evaluations: Optional[List[str]] = None
            guess_used: Optional[str] = None
            for (guess, _h) in suggestions:
                # Dismiss overlays between turns; sometimes share/login popups appear mid-game.
                _dismiss_overlays(page, log_debug=log_debug if debug else None, aggressive=False)
                evs = _try_submit_guess(
                    page,
                    guess,
                    row_index=turn - 1,
                    eval_timeout_s=args.eval_timeout,
                    log=log,
                    log_debug=log_debug if debug else None,
                )
                # If we didn't get evals, try one aggressive overlay sweep and retry the SAME guess once.
                if evs is None:
                    _dismiss_overlays(page, log_debug=log_debug if debug else None, aggressive=True)
                    evs = _try_submit_guess(
                        page,
                        guess,
                        row_index=turn - 1,
                        eval_timeout_s=args.eval_timeout,
                        log=log,
                        log_debug=log_debug if debug else None,
                    )
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
            log(f"turn {turn}: accepted guess '{guess_used}'")
            log(f"turn {turn}: evaluations={evaluations} pattern={pattern}")

            turns.append({"turn": turn, "guess": guess_used, "evaluations": evaluations, "pattern": list(pattern)})

            if pattern == (2, 2, 2, 2, 2):
                msg = f"Solved: {guess_used} in {turn} turns"
                print(msg)

                if args.result_path:
                    out_path = _expand_path(args.result_path)
                    payload = {
                        "date": time.strftime("%Y-%m-%d"),
                        "timestamp": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
                        "solved": True,
                        "answer": guess_used,
                        "turns": turn,
                        "steps": turns,
                    }
                    _write_json(out_path, payload, log=log if verbose else None)

                if args.notify:
                    _try_notify_send(msg, log=log if verbose else None, log_debug=log_debug if debug else None)
                return 0

            before = len(solver.candidates)
            solver.filter_candidates(guess_used, pattern)
            after = len(solver.candidates)
            log(f"solver: filtered candidates {before} -> {after}")
            if not solver.candidates:
                print("No candidates remain after filtering; extraction may be wrong or word lists mismatch.")
                return 6

        msg = f"Failed to solve within {args.max_turns} turns. Remaining candidates: {len(solver.candidates)}"
        print(msg)

        if args.result_path:
            out_path = _expand_path(args.result_path)
            payload = {
                "date": time.strftime("%Y-%m-%d"),
                "timestamp": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
                "solved": False,
                "answer": None,
                "turns": args.max_turns,
                "remaining_candidates": len(solver.candidates),
                "steps": turns,
            }
            _write_json(out_path, payload, log=log if verbose else None)

        if args.notify:
            _try_notify_send(msg, log=log if verbose else None, log_debug=log_debug if debug else None)
        return 7


if __name__ == "__main__":
    raise SystemExit(main())
