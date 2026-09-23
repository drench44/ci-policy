# ci-policy

Shared CI policy for drench44 repos. GitHub Free cannot enforce branch
protection on private repos, so these pieces do the job instead: checks that
run on every PR, a watchdog that alerts when something reaches main without a
green PR, local guards that stop pushes to main before they happen, and one
deploy script with a real health gate for the homelab services.

| Piece | Where | What it does |
| --- | --- | --- |
| PR policy | `.github/workflows/pr-policy.yml` (reusable) | Band stated, regression fix has a test, size limit |
| Main watch | `.github/workflows/main-watch.yml` (reusable) | Issue (emailed) when a commit reaches main without a merged, green PR |
| Allowlist | `policy/main-allowlist.json` | The only commits allowed on main without a PR. Read by main watch, the git pre-push hook, and the Claude Code push guard |
| Git hooks | `git-hooks/dispatch`, `install/install-git-hooks.sh` | pre-commit: no em dashes, no secrets. pre-push: no pushes to main/master. Chains to each repo's own hooks |
| Deploy | `deploy/homelab-deploy`, `deploy/deploy-lib.sh` | Tag, deploy, real health check, automatic rollback, rollback point in git |

Everything is Python standard library and bash, so it runs on GitHub's
runners, the self-hosted OMEN runners, the Mac (stock bash 3.2) and Linux.

## PR policy

Add `examples/pr-policy.yml` to a repo as `.github/workflows/pr-policy.yml`.
Private repos pass `runs-on: '["self-hosted","omen"]'` (hosted minutes are used
up); public repos pass `'"ubuntu-latest"'`.

Rules, for PRs opened by `drench44` (other people and bots pass with a notice):

1. **Review band.** The body says which band ran, in any wording the PR hook
   already produces: `Review band: one agent (code-reviewer), ordinary code`,
   `All three review agents ran`, `Review-band: 0 (docs only)`.
2. **Regression fixes need a test.** If the title says "regression"/"regressed",
   or the body says it fixes one ("fixes a regression from #88", "broken in
   #1089"), some test file must be added or changed. "No regressions",
   "regression risk" and "regression suite" do not count as a claim. Rare real
   exception: a `Regression-test: <why none>` line or the `no-regression-test`
   label (turns the failure into a warning).
3. **Size.** Changed lines, not counting lockfiles, generated, vendored,
   snapshot and fixture files, or anything `.gitattributes` marks
   `linguist-generated` or `linguist-vendored`. Over 800: warning. Over 2,500:
   fails unless the `size-override` label is on the PR or the body has a
   `Size-override: <reason>` line.

The job summary explains every result. The check re-runs when the body or
labels change.

## Main watch

Add `examples/main-watch.yml` as `.github/workflows/main-watch.yml` (branch
`master` for repos that use it). On every push to the default branch, each new
commit must belong to a PR that was merged into that branch with all head
checks green, or match `policy/main-allowlist.json`. Anything else, and any
force push or branch deletion, opens one issue labeled `main-watch` (later
alerts comment on it). GitHub emails the owner. It never reverts anything.

### The allowlist

Found on 2026-09-22 by reading every repo's scripts, workflows, timers and the
garage deploy units:

| Repo | Automation | Rule |
| --- | --- | --- |
| family-hub | `scripts/release.py`, pushed by hand with `git push --follow-tags` | subject `release: vX.Y.Z`, only `VERSION`, `CHANGELOG.md`, `src/family_hub/web/static/index.html` |
| house-climate | `scripts/release.py`, same flow | subject `release: vX.Y.Z`, only `VERSION`, `CHANGELOG.md`, `src/house_climate/web/static/*.html` |
| claude-config-backup (`~/.claude`) | config and memory backup commits straight to `master` | subject starts `backup: `, `memory: ` or `skills: ` |

Every other automation pushes feature branches and lands through PRs (the
cpapclarity articlewatch and sitestats timers, Dependabot). Nothing else
commits to main. Change the list through a PR here; all three guards pick it
up (reinstall the git hooks to refresh the local copy).

## Git hooks (every machine)

```sh
install/install-git-hooks.sh                    # Mac: every repo
install/install-git-hooks.sh --scope ~/workspace  # OMEN: only the dev clones
install/install-git-hooks.sh --check            # report problems, exit 1 if any
install/install-git-hooks.sh --uninstall        # undo, restoring repo hooks
```

The installer copies the code to `~/.config/ci-policy`, writes a wrapper for
every git hook name into `~/.config/git/hooks`, and points `core.hooksPath` at
it. On the OMEN the scope keeps the automation clones (articlewatch and the
other timers, which commit as the same user outside `~/workspace`) untouched.

- **pre-commit** blocks an em dash in any added line, a secret-shaped string
  (GitHub, AWS, Anthropic, OpenAI, Slack, Stripe, Google, Supabase and npm
  keys, private keys, JWTs such as Home Assistant tokens), and staged files
  named like secrets (`.env`, `id_rsa`, `*.p12`, ...; `.env.example` is fine).
  Escape hatches: `ci-policy: allow-secret` on a line with a fake key;
  `git config --add ci-policy.emdashAllow '<glob>'` or `linguist-vendored` for
  vendored upstream files; `git config --add ci-policy.secretAllow '<glob>'`.
- **pre-push** refuses any push that updates or deletes `main`/`master`,
  including `HEAD:main`, `refs/heads/main` and force pushes, unless every
  pushed commit is on the allowlist. Creating `main` on an empty remote (a new
  repo) is allowed. Humans can break glass with `CI_POLICY_ALLOW_MAIN_PUSH=1`.
- **Chaining.** `core.hooksPath` makes git ignore `.git/hooks`, which would
  silently turn off each repo's own hooks. Every wrapper runs the repo's hook
  of the same name after the policy passes, with the same arguments and stdin.
  Repos that set their own `core.hooksPath` (family-hub, house-climate and
  3ddesignlab use `.githooks`, cpapclarity its `.git/hooks`) would otherwise
  shadow the global setting, so the installer moves that value to
  `ci-policy.chainHooksPath` and the dispatcher chains there. `--check` finds
  any repo that sets `core.hooksPath` again later (for example by rerunning
  its own `scripts/install-hooks.sh`, or `npm install` re-running husky in
  lizardartist and riseyourway); rerun the installer to fix it.
  `CI_POLICY_DEBUG=1 git hook run pre-commit` shows which repo hook a repo
  chains to.

Installed 2026-09-22 on the Mac (global) and the OMEN (`--scope ~/workspace`).
Machine-local exception on both: weather-dashboard's vendored
`console_live.html` is exempt from the em dash check
(`ci-policy.emdashAllow`). Marking it `linguist-vendored` in that repo's
`.gitattributes` would make the exception travel with the repo.

## Deploy

`deploy/homelab-deploy <config>` runs one deploy from a per-repo config file
(`deploy/example.deploy.conf`; every setting is documented at the top of
`deploy/deploy-lib.sh`):

1. Refuses a shallow health gate. The config must name fields `/health` has
   to carry (`DL_HEALTH_REQUIRE`) and/or a data timestamp that must be recent
   (`DL_HEALTH_FRESH`). An endpoint that only says ok is how a deploy deleted
   a box's `.env` and a card vanished for a day.
2. Records the rollback point: tags each running image `<image>:pre-<sha>`,
   snapshots the compose directory on the box, and pushes git tag
   `rollback-point/<service>/<time>` on the commit the box was running.
3. Runs the config's `dl_sync`, then checks required files and env values on
   the box (`DL_REQUIRE_FILES=".env"`, `DL_REQUIRE_ENV=".env:HA_TOKEN"`)
   before anything restarts.
4. `docker compose up -d --build`, then polls health.
5. Healthy: records the commit on the box, pushes `deploy/<service>/<time>-<sha>`.
   Unhealthy: restores the files and images, recreates without building,
   checks health again, pushes `failed-deploy/<service>/<time>-<sha>`.

Exit codes: 0 ok, 1 rolled back and healthy, 2 down (manual command printed),
3 refused before restarting, 4 deployed but a tag push failed.
`homelab-deploy <config> --health` runs the gate once against what runs now.

## Tests

```sh
cd tests && python3 -m unittest discover -s .   # policy, watch, allowlist, hook checks
bash tests/deploy/run-tests.sh                  # deploy library (stubbed docker/ssh/curl)
bash tests/git-hooks/run-tests.sh               # installer, dispatcher, hooks, real git
```

CI runs all three on Ubuntu and macOS (stock bash 3.2), plus shellcheck,
actionlint and an em dash check. This repo also runs its own PR policy and
main watch from the commit under test.
