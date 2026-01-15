# What's my Wordle?

Wordle solver using information theory (expected information gain / entropy).

This repo contains:
- wordle.py: interactive helper that suggests the next guess
- wordle_tester.py: simulator that runs many games and prints aggregate statistics 


## Interactive solver (wordle.py)

Run with:

`python3 wordle.py --words official_allowed_guesses.txt --answers shuffled_real_wordles.txt`

Each turn it prints top suggestions, then asks for the feedback pattern:
- g/y/b (green/yellow/black), e.g. bygyb

The first-turn scoring is cached on disk in .first_guess_entropy_cache.json next to wordle.py. This is to speed up repeated runs since the first turn is the slowest, and it's always the same.

## Simulator / stats (wordle_tester.py)

To run:
`python3 wordle_tester.py --words official_allowed_guesses.txt --answers shuffled_real_wordles.txt --guess-space candidates --plot results.png`

## Output
Simulating: 100%|█████████████████████████████████████████████████████████████████████████████████████████████████████████████████████████████████| 2315/2315 [01:11<00:00, 32.40game/s]
Games: 2315
Solved: 2303 (99.48%)
Failed: 12 (0.52%)
Avg turns (solved): 3.582
Median turns (solved): 4.0
Turn distribution (solved): 1:1, 2:131, 3:999, 4:919, 5:205, 6:48
Most common first guess: raise (2315 / 2315)
Failed examples (up to 10): boxer, tacky, water, latch, waste, foyer, grade, watch, creak, tight
Wrote plot: results.png

## Plot
The plot shows the distribution of number of turns taken to solve the puzzles, with a bar for each turn count (1 to 6) and a separate bar for failures. The count of games for each bar is shown on top of the bar.

![results.png](results.png)