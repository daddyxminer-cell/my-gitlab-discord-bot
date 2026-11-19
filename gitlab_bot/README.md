# GitLab PoW coin / node project -> Discord bot (v3)

Finds **any** GitLab project - brand new or years old - that is a **Proof-of-Work coin** or a **project that needs you
to run a node** (masternode, validator, incentivised testnet) and that **entered `pre-testnet`, `testnet` or launched its
`mainnet` within the last 7 days** (`RECENT_DAYS`). Each project is posted once **per stage**:

```
New record added: https://gitlab.com/some-group/some-coin
-# PoW coin · testnet · RandomX
-# found: chainparams, genesis, pow.cpp, RandomX
-# when: testnet event 2.8d ago (diff docs/testnet.md @e40cfbab), last commit 2.5d ago
```

## What changed in v3

| v2 | v3 |
| --- | --- |
| Only looked at projects **created** in the last 48 h | Two streams: **created** projects + recently **active** projects (old repositories included) |
| Skipped imported / old repositories (`MAX_HISTORY_DAYS`) | **No repository-age rule at all.** Only the latest commit or release/tag must be within 7 days |
| Skipped every mainnet project | Mainnet is **dated**: launched within 7 days -> posted, older -> skipped |
| One post per project | One post per project **and stage** (`123_testnet`, then `123_mainnet`) |
| `--explain` showed a score | `--explain` shows stage, commit / release age, mainnet launch age in days, events, why pass / fail |

### Discovery (two streams, de-duplicated by project id)

1. **Created stream** - every public project created in the last `MAX_AGE_HOURS` (id window + `last_activity_after`).
2. **Active stream** - `GET /api/v4/projects?order_by=last_activity_at&sort=desc&last_activity_after=<now-7d>&search=<kw>` for
   each keyword in `testnet, mainnet, pre-testnet, genesis, pow, randomx, masternode, validator`
   (`ACTIVE_KEYWORDS`). At most `ACTIVE_MAX_PAGES` x 100 projects per keyword (rate-limit friendly; `pow` is very broad).
   The active cursor only moves after a complete run, with a 3 h overlap.

Every candidate is read the same way (file list, README, key files) and scored; only projects that look like a real PoW /
node chain go on to the recency check (3 cheap requests: commits, tags, releases; a few more for diffs).

### Recency check (`recency.py`)

* **Activity gate** - latest commit **or** latest release/tag <= 7 days, otherwise skipped.
* **Stage events inside the 7 days** (needed for repositories that were not created this week, so "someone fixed a typo" does not count):
  * releases / tags: name + notes parsed for `testnet, pre-testnet, devnet, mainnet, launch, genesis`
  * commit messages (a weak hint, not enough alone)
  * diffs of the last 7 days, **added lines only**, in chain files (`chainparams`, `genesis*`, `chain_spec`, `*testnet*`, `config.toml` ...)
    and docs: a new genesis hash, a new testnet config / seed list, "testnet is live" text. A genesis hash that merely moved, regtest, and tests are ignored.
* **Mainnet rule** - the launch date is, in this order: a fresh genesis in a recent diff -> the earliest launch release/tag
  (`mainnet`, `v1.0.0`, notes "mainnet launched") -> the commit that first introduced the genesis / chainparams file -> repository creation date (last resort).
  **<= 7 days: posted.  Older: skipped.  A later "mainnet launched" note never re-dates an old launch.**
  A mature mainnet project that opens a *new testnet* this week is posted as `testnet`.
* **Noise filters stay on**: tutorials, captchas, pure ERC-20 / SPL tokens, generic explorers / wallets / miners / pools, unmodified
  forks. A fork is accepted only when it shows its own recent launch event; mirrors never.

## How a project is judged

Three kinds of evidence, all listed in `patterns.py`:

* **TEXT** - README / docs / description / topics (RandomX, block reward, masternode, "run a validator" ...)
* **TREE** - file names (`chainparams.cpp`, `pow.cpp`, `masternode.cpp`, `genesis.json`, `randomx/`, Bitcoin-fork layout ...)
* **CODE** - contents of key files (`nPowTargetSpacing`, `pchMessageStart`, `GetBlockSubsidy`, `bootnodes`, validator-set code ...)

It must look like a **real chain** (not just contain the word "PoW"), and it is rejected if it is a tutorial /
"blockchain in Python" demo, captcha-style proof-of-work, an ERC-20 / meme token, a miner / pool / explorer / wallet **tool**,
a trading bot, an unmodified copy of Bitcoin/Monero, or an unmodified fork without a launch of its own.

Stage detection starts from the text (`mainnet`: exchange listings, mainnet peers / RPC ...; `testnet`: faucet, join the testnet ...;
otherwise `pre-testnet`) and is then corrected by the dated events above.

## Commands

```
python bot.py --once --dry-run          # test: prints what it would post, sends and saves nothing
python bot.py --once                    # check once (cron / GitHub Actions)
python bot.py                           # run forever, every CHECK_INTERVAL_MINUTES
python bot.py --explain GROUP/PROJECT   # detected stage, commit / release age, mainnet launch age (days), events, why it passes / fails
python bot.py --discover 45             # look back 45 days using GitLab search + deep read; changes nothing
python bot.py --near-misses             # projects that almost matched (from the saved state)
python tests/test_flow.py               # offline test of posting / no-repeat / retry / damaged-state logic
python tests/test_recency.py            # offline test of the 7-day rules, mainnet dating, stage-keyed state, active stream
```

## Never posts the same project twice

* Saved as "posted" **before** the message is sent (a crash can lose a message, never repeat one).
* Remembered forever **per stage** (`<project id>_<stage>` and `<group/name>_<stage>`). A project posted at `testnet` is posted again
  only when it reaches `mainnet`; never twice at the same stage. Old state files (v2) are migrated automatically.
* One post per owner for `ONE_PER_OWNER_DAYS` (default 30) - a project you already posted may still move to its next stage.
* Atomic state file + backup; if both copies are damaged the bot stops instead of starting empty.
* Only one copy can run at a time (`state/bot.lock`).
* **First start looks from NOW**: it posts nothing for projects that already exist (`POST_ON_FIRST_RUN=true` to change).

## Self-healing

* Retries with exponential backoff + jitter; all workers pause together on a rate limit.
* A project that cannot be read goes to a **retry queue** and is tried again every run (up to 8 times).
* The listing cursor only moves forward after a **complete** run, so nothing is skipped after a failure.
* If `GITLAB_TOKEN` is rejected (expired / revoked) it keeps running anonymously and tells you.
* One bad project never stops a run. The loop mode never dies.
* `ALERT_WEBHOOK_URL` (optional second webhook) gets: repeated failures, token problems, a daily near-miss digest, and an
  optional "no match for N hours" notice (`SILENCE_ALERT_HOURS`).
* `RUN_BUDGET_MINUTES` stops a run cleanly before a CI time limit; unfinished projects are read next run.

## Setup

1. **Discord webhook**: Edit Channel -> Integrations -> Webhooks -> New Webhook -> Copy URL.
2. **GitLab token** (strongly recommended): Preferences -> Access tokens, scope `read_api`. 2000 requests/min instead of 500.
   *Never paste a token into chat or commit it.* Put it in `.env` (git-ignored) or a GitHub secret.
3. **Run it**
   * GitHub Actions: copy `gitlab_bot/github-workflow.yml` to `.github/workflows/gitlab_pow_bot.yml`, add secrets
     `DISCORD_WEBHOOK_URL_GITLAB`, `GITLAB_TOKEN` (optional: `ALERT_WEBHOOK_URL`, `LLM_API_KEY`). Memory lives on branch `gitlab-bot-state`.
   * PC / VPS: `pip install -r requirements.txt`, `cp .env.example .env`, edit it, `python bot.py --once --dry-run`, then `python bot.py`.

## Settings

| Variable | Default | Meaning |
| --- | --- | --- |
| `DISCORD_WEBHOOK_URL` | - | Discord webhook (required) |
| `GITLAB_TOKEN` | - | GitLab token (`read_api`) |
| `ALERT_WEBHOOK_URL` | - | Optional second webhook for health messages and the daily near-miss digest |
| `STAGES` | `pre-testnet,testnet,mainnet` | Which stages to post |
| `RECENT_DAYS` | 7 | A stage change / mainnet launch must be at most this old (0 = recency check off) |
| `REQUIRE_EVENT` | true | Repos created before the window need a dated testnet / genesis / launch event, not just activity |
| `ACTIVE_STREAM` | true | Also poll recently active projects (old repositories) |
| `ACTIVE_KEYWORDS` | `testnet,mainnet,pre-testnet,genesis,pow,randomx,masternode,validator` | Searches of the active stream |
| `ACTIVE_MAX_PAGES` | 5 | Max pages (x100 projects) per keyword per run |
| `WATCH_HOURS` | 12 | A chain-like project is re-read on new activity at most this often |
| `MIN_SCORE` | 8 | Higher = stricter |
| `INCLUDE_NODE` | true | Also post node / masternode / validator projects |
| `MAX_AGE_HOURS` | 48 | Window of the **created** stream |
| `ONE_PER_OWNER_DAYS` | 30 | One post per owner/group for N days (0 = off) |
| `WORKERS` | 6 | Parallel readers (max 16) |
| `RUN_BUDGET_MINUTES` | 0 | Stop a run after N minutes (0 = no limit) |
| `BLOCK_OWNERS` | - | Comma-separated owners/groups to ignore |
| `SHOW_DETAILS` | true | Add the type / stage / algorithm / evidence lines |
| `SILENCE_ALERT_HOURS` | 0 | Alert if nothing matched for N hours (0 = off) |
| `LLM_API_KEY` | - | Optional AI judge for borderline cases (OpenAI-compatible; `LLM_BASE_URL`, `LLM_MODEL`) |
| `POST_ON_FIRST_RUN` | false | Post what is found on the very first run too |

To tune what counts, edit `patterns.py`, then check with `--explain`.

## Known limits (be aware)

* Stage and "real chain" are judged from text/files, so a project can be misjudged. Use `--explain`, and `--near-misses`.
* Mainnet dating relies on genesis / chainparams history, tags and release notes. A project with none of these is dated by its
  creation date (so an old repo with an undated mainnet is skipped, not guessed).
* The `pow` active query is capped at `ACTIVE_MAX_PAGES` pages; raise it if you have a token and want full coverage.
* A project with an empty repository or no README yet is re-read after its next push, only while it is within `MAX_AGE_HOURS`.
* Private projects and non-public hosts are not visible. Self-managed GitLab works via `GITLAB_URL`.
