# GitLab New PoW / Node Project -> Discord Bot

Searches **GitLab.com** for **brand-new projects** that look like a mineable **Proof-of-Work coin** or a **node / masternode project**, and posts each one to Discord once, like this:

```
New record added: https://gitlab.com/some-group/some-coin
```

Discord turns the link into the GitLab preview card on its own.

This is the GitLab version of `github_bot` (same scoring, same no-repeat memory). The bot itself can still run on GitHub Actions, so GitHub hosts it and GitLab is what it watches.

## How it works

1. Every run it searches GitLab for projects created in the last 48 hours, using PoW words (`proof of work`, algorithm names like RandomX / KawPow / Ethash / Scrypt / kHeavyHash...), `masternode`, `genesis block`, `block reward`, `fair launch`, `mining pool`, and topics like `pow`, `mining`, `cryptocurrency`, `blockchain`, `masternode`.
2. For each new project it reads the README and gives it a **score**:
   * **+** proof-of-work words, algorithm names, mining/pool/hashrate
   * **+** coin/chain signs: genesis block, block reward, block time, supply, halving, premine, mainnet/testnet, wallet, explorer, node software
   * **+** node signs: masternode, node rewards, run a node, collateral
   * **-** noise: captchas/anti-bot proof-of-work, password hashing, tutorials/homework/demos, portfolios, ERC20/meme tokens, trading bots
3. Projects scoring at least `MIN_SCORE` (default 7) that look like a real chain are posted. Forks are skipped.

> GitLab's project search only looks at **name and description**, not the README (GitHub's can). So the README score is what really decides, and a coin whose name/description mentions none of the search words can be missed.

To see why a project was or wasn't posted:

```
python bot.py --explain some-group/some-coin
#   score 12 (need 7)  ->  POST as POW
#   +pow:algorithm +pow:proof-of-work +chain:block time +chain:genesis ...
```

## Never posts the same project twice

* A project is saved as "posted" **before** the message is sent, so a crash or restart can't repeat it.
* Posted projects are remembered forever, by project ID and by group/name.
* **One post per owner:** after posting a project, other projects from the same user/group are skipped for 30 days (`ONE_PER_OWNER_DAYS`).
* New activity in an already-posted project never posts it again.
* A project with no README yet is checked again after its next activity, and posted then if it qualifies.
* If the bot starts with no memory, it only remembers what's out there and posts nothing.
* The memory file is saved safely with a backup. If both copies are damaged, the bot stops.
* Only one copy can run at a time. **Run the bot in one place only.**

## Setup

### 1. Discord webhook

Discord channel -> **Edit Channel -> Integrations -> Webhooks -> New Webhook -> Copy Webhook URL**.

### 2. GitLab token (recommended)

gitlab.com works without a token, but it rate-limits anonymous clients **per IP**, and GitHub Actions runners share IPs with many other jobs. If the log says `GitLab rate limit reached`, add a token:

GitLab -> **Preferences -> Access tokens -> Add new token**, scope `read_api`. This is a different token from the GitHub one.

When rate limited the bot does not crash or repeat anything: it works with what it found and checks the rest next run.

### 3a. GitHub Actions (runs 24/7, no PC needed)

1. On GitHub, create `.github/workflows/gitlab_pow_bot.yml` (**Add file -> Create new file**) and paste in the contents of `gitlab_bot/github-workflow.yml`. (It is added by hand because pushes from automated tools are not allowed to create workflow files.)
2. **Settings -> Secrets and variables -> Actions -> New repository secret**:
   * `DISCORD_WEBHOOK_URL_GITLAB` = webhook for this bot's channel. If you leave it out, the bot uses `DISCORD_WEBHOOK_URL`.
   * `GITLAB_TOKEN` = optional GitLab token from step 2.
3. **Actions -> "GitLab PoW project notifier" -> Run workflow**. The first run only remembers what's already out there. After that it posts new projects every hour.

Memory is kept on its own branch, `gitlab-bot-state`.

### 3b. Your own PC or VPS

```
cd gitlab_bot
pip install -r requirements.txt
cp .env.example .env              # paste your webhook URL (and a GitLab token) into .env
python bot.py --once --dry-run    # test: prints what it would post, sends nothing
python bot.py                     # runs forever, checks every 30 minutes
```

## Settings

| Variable | Default | Meaning |
| --- | --- | --- |
| `DISCORD_WEBHOOK_URL` | - | Discord webhook (required) |
| `GITLAB_TOKEN` | - | GitLab token (`read_api`); avoids anonymous rate limits |
| `GITLAB_URL` | https://gitlab.com | GitLab server (self-managed works too) |
| `BOT_NAME` | webhook name | Name shown in Discord |
| `CHECK_INTERVAL_MINUTES` | 30 | Loop interval when not using `--once` |
| `MAX_AGE_HOURS` | 48 | Only projects created in the last N hours |
| `MAX_PAGES` | 3 | Result pages (100 projects each) read per search |
| `MIN_SCORE` | 7 | Higher = stricter (try 9-10 if you get noise) |
| `INCLUDE_NODE` | true | Also post node/masternode projects |
| `ONE_PER_OWNER_DAYS` | 30 | Skip the same owner's other projects for N days (0 = off) |
| `MIN_STARS` | 0 | Ignore projects with fewer stars |
| `SHOW_DETAILS` | false | Add a small 2nd line: type · algorithm |
| `POST_ON_FIRST_RUN` | false | Post matches on the very first run too |

To tune what counts as a match, edit `SEARCH_WORDS`, `SEARCH_TOPICS`, `POW_PATTERNS`, `CHAIN_PATTERNS`, `NODE_PATTERNS` and `NEG_PATTERNS` in `bot.py`. Use `--explain GROUP/PROJECT` and `-v` to see the scores.

To reset the memory, delete `state/seen.json` and `state/seen.json.bak` (or, on GitHub, the `gitlab-bot-state` branch).
