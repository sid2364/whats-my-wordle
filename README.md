# What's my Wordle?

Wordle solver using information theory (expected information gain/entropy).

This repo contains:
- src/solver/wordle.py: interactive helper that suggests the next guess
- src/solver/wordle_tester.py: simulator that runs many games and prints aggregate statistics 
- src/bot/nyt_wordle_bot.py: automated bot that plays Wordle on the New York Times website using Playwright
- src/bot/wordle_last.py: utility to print the last saved result from the bot (for use in shell prompts, notifications, etc.)


## Setup

Install Python deps:

```bash
python3 -m pip install -r requirements.txt
```
or 

```bash
pip install -r requirements.txt
```

For the bot, install the Playwright browser binary (one-time). Not needed for the solver or tester!

```bash
python3 -m playwright install chromium
```

## Usage: Interactive solver (src/solver/wordle.py)

Run with:

```bash
python3 src/solver/wordle.py --words official_allowed_guesses.txt --answers shuffled_real_wordles.txt
```

Each turn, it prints the top suggestions, then asks for the feedback pattern:
- the guess you used
- g/y/b (green/yellow/black), e.g. bygyb

The first-turn scoring is cached on disk in .first_guess_entropy_cache.json next to wordle.py. This is to speed up repeated runs since the first turn is the slowest, and it's always the same.


### Command line interface
```
=== Wordle Entropy Solver ===
Allowed guesses: 10657
Possible answers: 2315
Feedback input: 5 letters [g,y,b] or digits [2,1,0]. Example: bygyb or 02120
Type 'quit' to exit.

Turn 1 | Remaining candidates: 2315

Top suggestions (guess | expected bits):
  soare  |  5.8860
  roate  |  5.8828
  raile  |  5.8657
  reast  |  5.8655
  salet  |  5.8346
  orate  |  5.8172
  carte  |  5.7946
  raine  |  5.7867
  caret  |  5.7767
  ariel  |  5.7752

Suggested guess: soare

Enter the guess you used (or press Enter to use suggested): 
...
```
After each turn, the CLI shows how many candidate answers remain, the top suggested guesses, and each guess’s expected information gain (in bits), which quantifies how informative the guess is. The higher the bits, the better. But later turns often have lower bits since there are fewer candidates left, so if we have a value of 0, then we know we have found the answer.

## How does it work?

The solver treats Wordle like a search problem: there’s an unknown secret word `s`, and we maintain a set of remaining possible secrets (the “candidates”). When you play a guess `g`, Wordle returns a 5-slot feedback pattern. A “good” guess is one that, on average, splits the candidates into lots of smaller groups so the next step is easier.

Feedback is represented as five integers (one per position): `2` = green (right letter, right spot), `1` = yellow (right letter, wrong spot), `0` = gray (letter not present, or present but you guessed it too many times). There are $3^5 = 243$ possible patterns. Repeated letters matter: Wordle is *counted* membership, not set membership. The implementation in [src/solver/wordle.py](src/solver/wordle.py) matches the official behavior by doing two passes: first mark greens and decrement a per-letter counter from the secret, then mark yellows only if that letter still has remaining count.

To score a guess, assume the secret is uniformly distributed over the current candidates (so each remaining word is equally likely). For a given guess `g`, we simulate `pattern = feedback(s, g)` for every candidate secret `s` and bucket secrets by their resulting pattern. If $N$ candidates remain and $c_p$ of them produce pattern $p$, then $\sum_p c_p = N$ and the probability of observing $p$ is $P(p) = c_p / N$. The guess’s “expected bits” is the Shannon entropy of this pattern distribution:

$$
H(g) = -\sum_p P(p)\,\log_2 P(p)
$$

Intuitively, higher entropy means you expect more information: the guess tends to spread candidates across many patterns instead of collapsing into one common outcome.

Each turn, the solver evaluates guesses from a pool and picks the one with the highest entropy. The pool is controlled by `--guess-space`: `candidates` only scores words that could still be the secret, while `allowed` also considers “probe” guesses that aren’t valid secrets but can be more informative. After you type the real pattern you got from Wordle, the solver updates the state by filtering candidates to exactly those secrets `s` where `feedback(s, guess) == observed_pattern`, then repeats the loop.

Performance-wise, scoring is roughly $O(|pool| \cdot |candidates|)$ feedback computations per turn. This repo speeds things up with in-memory memoization of `feedback(secret, guess)` and by caching the full first-turn entropy table to `.first_guess_entropy_cache.json` (turn 1 is always the same candidate set). The tester in [src/solver/wordle_tester.py](src/solver/wordle_tester.py) runs the same loop automatically across many secrets and aggregates stats.

### Example entropy iteration

Pretend we’re mid-game and we’ve already narrowed things down to just 4 possible answers:

```
candidates = {cigar, rebut, sissy, humph}
N = 4
```

Now we’re considering a guess `g`. We don’t know the secret, so we “try” `g` against each candidate, record the resulting feedback pattern, and count how often each pattern happens.

Say `g` produces these buckets:

```
pattern A: 2 candidates
pattern B: 1 candidate
pattern C: 1 candidate
```

A pattern here is just a specific 5-slot feedback like `gybby`. A word resulting in different patterns goes into different buckets - we know the patterns possible because we know the candidates!

Here, that means the probabilities are $P(A)=2/4=0.5$, $P(B)=1/4=0.25$, $P(C)=1/4=0.25$. The entropy is:

$$
H(g) = -(0.5\log_2 0.5 + 0.25\log_2 0.25 + 0.25\log_2 0.25)
  = -(0.5\cdot(-1) + 0.25\cdot(-2) + 0.25\cdot(-2))
  = 1.5\ \text{bits}
$$

Translation: on average, this guess is worth about 1.5 bits of “narrowing-down power”. Often you’ll cut the candidate set roughly in half (or better), because different feedback patterns point to different subsets. 1 bit of information halves the search space, 2 bits quarters it, etc. So 1.5 bits is pretty good coz it means you’re doing better than halving each turn. Initially, with 2300 candidates, the entropy of a good guess is around 5-6 bits, which cuts the candidates down to about 50-100 after the first turn (statistically!), and after the second turn you’re often down to just a few dozen considering an information gain of 3-4 bits. This game works so well because the branching factor is high: there are 243 possible feedback patterns, so good guesses can split candidates into many small groups. And you cannot get “stuck” in a bad state because every guess gives some information (no zero-entropy guesses). Initially you need around 11-12 bits to uniquely identify one word out of 2315 (which isn't possible with 5 letter guesses), so with good guesses you can expect to solve most puzzles in about 4-5 turns.

So now, compare our guess with a more "meh" guess `g2` that buckets like this:

```
pattern X: 3 candidates
pattern Y: 1 candidate
```

Here $P(X)=0.75$ and $P(Y)=0.25$, so $H(g2) \approx 0.811$ bits. That’s lower because most of the time you get the same pattern (X), which doesn’t narrow things down much.

After you actually play the guess and Wordle shows you a concrete pattern (say you got pattern B), the solver just filters:

```
candidates := {s in candidates | feedback(s, g) == pattern_B}
```

...and repeats with the smaller set.

Ultimately, the goal is to maximize information gain each turn, so you reach a single candidate (the secret) as quickly as possible. 

## Usage: Simulator/stats (src/solver/wordle_tester.py)

To run:
```bash
python3 src/solver/wordle_tester.py --words official_allowed_guesses.txt --answers shuffled_real_wordles.txt --guess-space candidates --plot results.png
```

## Output
```
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
```

## Plot
The plot shows the distribution of the number of turns taken to solve the puzzles, with a bar for each turn count (1 to 6) and a separate bar for failures. The count of games for each bar is shown on top of the bar.

![results.png](results.png)


## NYT Wordle Bot

The bot `src/bot/nyt_wordle_bot.py` uses Playwright to control a Chromium browser instance. It automatically inputs guesses and reads feedback from the page, allowing it to solve the puzzle without manual input. This is specifically designed for the New York Times Wordle web interface.

### Setup

Install the Playwright browser binary (one-time):

```bash
python3 -m playwright install chromium
```

### Usage (src/bot/nyt_wordle_bot.py)

Non-headless (so that you can see what it’s doing):

```bash
python3 src/bot/nyt_wordle_bot.py --words official_allowed_guesses.txt --answers shuffled_real_wordles.txt
```

Headless:

```bash
python3 src/bot/nyt_wordle_bot.py --headless
```

Notes:
- NYT can change the Wordle page DOM; if that happens, the scraper in `src/bot/nyt_wordle_bot.py` may need updates.
- If guesses are being rejected, the local word list may include words NYT no longer accepts; the bot will automatically try the next suggestion.

### Saving the result (for scheduling)

You can make the bot write the daily result to a file (JSON), which is handy for systemd timers:

```bash
python3 src/bot/nyt_wordle_bot.py --headless --result-path ~/.cache/wordle-bot/last.json
```

To print the last saved result later:

```bash
python3 src/bot/wordle_last.py
```

### Run daily with a systemd user timer (Linux)

This runs automatically whenever your laptop is on, and your user systemd session is running.

Important behavior:
- If your laptop is fully powered off at the scheduled time, nothing can run. With `Persistent=true`, the job runs once the next time your user systemd session is active again.
- If your laptop is sleeping/suspended, the job won’t run during sleep; it should run soon after you resume (again thanks to `Persistent=true`).
- If you want it to run even when you are not logged in, enable lingering (below).

1) Copy the unit + timer into your user systemd directory:

```bash
mkdir -p ~/.config/systemd/user
cp systemd/user/wordle-bot.service ~/.config/systemd/user/
cp systemd/user/wordle-bot.timer ~/.config/systemd/user/
```

2) If your repo is NOT at `~/Forge/whats-my-wordle`, edit the path in the service file:

```bash
systemctl --user edit --full wordle-bot.service
```

3) Enable and start the timer:

```bash
systemctl --user daemon-reload
systemctl --user enable --now wordle-bot.timer
systemctl --user list-timers --all | grep wordle
```

4) See logs and the last answer:

```bash
journalctl --user -u wordle-bot.service -n 200 --no-pager
python3 src/bot/wordle_last.py
```

#### Show the answer when you open a terminal

Add this to `~/.bashrc` (or `~/.zshrc`) to print the last saved result when you start a shell:

```bash
if [ -f "$HOME/.cache/wordle-bot/last.json" ]; then
  $HOME/Forge/whats-my-wordle/.venv/bin/python "$HOME/Forge/whats-my-wordle/src/bot/wordle_last.py" 2>/dev/null || true
fi
```
Note: desktop notifications (`--notify`) usually require an active graphical session.

### Bot output example

```
$ python3 src/bot/nyt_wordle_bot.py --words official_allowed_guesses.txt --answers shuffled_real_wordles.txt --first-guess salet --verbose
[   0.00s] solver: loaded allowed=10658 answers=2316 guess_space=candidates
[   0.00s] solver: forced first guess = salet
[   0.47s] browser: launching chromium headless=False slowmo=0ms
[   0.47s] browser: navigating to https://www.nytimes.com/games/wordle/index.html
[  14.64s] browser: clicked Play
[  14.68s] browser: detected Wordle DOM mode = react
[  14.68s] browser: focused game (clicked page)
[  14.68s] turn 1: candidates remaining = 2316
[  14.69s] solver: top suggestions = salet
[  15.55s] browser: typing guess 'salet' (row 1)
[  21.60s] browser: guess 'salet' appears rejected (no evaluation within 6.0s)
[  27.96s] browser: typing guess 'salet' (row 1)
[  29.46s] browser: got evaluations for row 1: ['correct', 'present', 'absent', 'absent', 'absent']
[  29.46s] turn 1: accepted guess 'salet'
[  29.46s] turn 1: evaluations=['correct', 'present', 'absent', 'absent', 'absent'] pattern=(2, 1, 0, 0, 0)
[  29.47s] solver: filtered candidates 2316 -> 32
[  29.47s] turn 2: candidates remaining = 32
[  29.48s] solver: top suggestions = scamp, spark, scrap, scram, sharp, smack, swamp, shark ...
[  30.34s] browser: typing guess 'scamp' (row 2)
[  31.89s] browser: got evaluations for row 2: ['correct', 'present', 'present', 'present', 'absent']
[  31.89s] turn 2: accepted guess 'scamp'
[  31.89s] turn 2: evaluations=['correct', 'present', 'present', 'present', 'absent'] pattern=(2, 1, 1, 1, 0)
[  31.89s] solver: filtered candidates 32 -> 1
[  31.89s] turn 3: candidates remaining = 1
[  31.89s] solver: top suggestions = sumac
[  32.77s] browser: typing guess 'sumac' (row 3)
[  34.32s] browser: got evaluations for row 3: ['correct', 'correct', 'correct', 'correct', 'correct']
[  34.32s] turn 3: accepted guess 'sumac'
[  34.32s] turn 3: evaluations=['correct', 'correct', 'correct', 'correct', 'correct'] pattern=(2, 2, 2, 2, 2)
Solved: sumac in 3 turns
```

## Word list source
https://github.com/Kinkelin/WordleCompetition/tree/main/data/official

## Original motivation
3Blue1Brown video: https://www.youtube.com/watch?v=v68zYyaEmEA
