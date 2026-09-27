# MVGP operator runbook (the owner's Mac)

One owner, one Mac. Run the commands below as the service user from a terminal on the Mac; none
of these operations is reachable from the web.

Set `MVGP_STUDIO` to your studio folder. If unset or empty, it defaults to `/Users/Shared/mvgp-studio`.
The examples below use the same default:

```sh
export MVGP_STUDIO="${MVGP_STUDIO:-/Users/Shared/mvgp-studio}"
```

The launcher resolves Cloudflare from `MVGP_CLOUDFLARED` (the executable path), then `cloudflared` on `PATH`.
It resolves the FFmpeg directory from `MVGP_FFMPEG_BIN`, then the directory containing `ffmpeg` on `PATH`.
Keep `ffprobe` beside `ffmpeg`. The existing Homebrew paths are last-resort fallbacks.

## Topology

| Piece | Where |
|---|---|
| Studio folder | `$MVGP_STUDIO` (private, 0700) |
| What runs | `current.json`: `code_dir` (`runtime/lean-<sha>`, read-only), `api_config`, `worker_config` (`runtime/lean-<sha>.config/`) |
| Launcher | `launch.py` (from the code dir's `studio/launch.py`); `launch-release.py` is the old release launcher, kept for a rollback to `runtime/release-97` |
| API | uvicorn on `127.0.0.1:8811`; the desk is `http://localhost:8811/review` on this Mac with `local_owner`. For a configured public origin such as `https://mvgp.example.com`, `launch.py tunnel live` starts the Cloudflare tunnel (token in `env/tunnel.token`) |
| Owner sign-in | none on this Mac: with `local_owner` the desk page itself gets the owner session (exact origin and the browser's `Sec-Fetch-Site`); an agent's bearer token is refused. Reopening the public address means an `owner` Access block and an https origin instead |
| Worker | `production.worker --config <worker_config>`: Higgsfield 1080p takes (the `higgsfield` CLI at `bin/hf`, signed in under `hf-home`, section `hf`), fal drafts and completions for films in 样片模式, apilio images, the Gemini reader, cuts, the film |
| Store | `state/live/metadata.sqlite` (WAL), `state/live/media`, `state/live/review-payloads` |
| Secrets | `env/live-api.json`, `env/live-worker.json` (0600). Never edited by the deploy; the launcher passes lean code only `MVGP_PORT`, `FAL_AI_TOKEN`, `APILIO_AI_KEY`, `MVGP_WORKER_TOKEN`, `MVGP_FILM_TOKEN` |
| Settings | `production/config/runtime.json` in the code dir: prices, routes, provider capabilities, cut policy, reader profile, methods. A change ships as a deploy |
| Operator config | `config/live/operator.json` (database, operator id, credential directory, public origin) |

Never print a secret; check presence with `printenv KEY >/dev/null && echo set`.

## Everyday commands

```sh
studio/ops.sh status                     # services, the code dir they run, local /review
studio/ops.sh deploy <commit>            # see below
studio/ops.sh restart                    # restart api and worker on the current code dir
studio/ops.sh rollback                   # previous code dir and config
studio/ops.sh reconcile-cost <file>      # settle one unknown charge from the provider's own bill
studio/ops.sh ledger                     # pull Higgsfield's, fal's and apilio's own records for the 账本 now
```

The 账本 (owner's 个人设置 panel) compares the platform's receipts with the providers' own records, which
`studio/usage_pull.py` saves to `state/live/ledger/<provider>-<day>.json` (0600) after every release and on
`ops.sh ledger`. fal's usage API needs the admin key `FAL_AI_ADMIN_TOKEN`, kept in `env/live-ledger.json` (0600); apilio reads `APILIO_AI_KEY` from `env/live-worker.json`. apilio reports only a running
total, so a day's apilio spend is the difference between two snapshots; nothing takes them on a schedule yet.
Higgsfield is read through the pinned CLI with the worker config's `hf` HOME (`account transactions`, `account
status`); its transactions name no job, so the 账本 matches each take to one spend of the same credits within 30
minutes and lists other account activity separately.

## Idle check

`python3 studio/idle_check.py` (read-only) exits 0 when nothing is queued, dispatching, running or held, 3 when busy.
Parked unknown jobs (a lost answer waiting for the operator) do not count as busy. A deploy refuses to run while busy.

## Deploy (minutes, never touches the database)

`studio/deploy.sh <commit>`:

1. `git archive` of `production studio` into `runtime/lean-<sha>` (read-only);
2. config beside it: migrated from the release-97 configs the first time (`studio/migrate_config.py`), afterwards a
   copy of the current configs (or a dir prepared ahead with a deliberate change); every config dir is checked by
   the new code's `studio/check_config.py` — its models and the worker's lease rule (lease > 2 × the longest
   provider timeout + 5 s) — before anything stops;
3. stop the worker with SIGTERM and wait (up to 15 minutes): the lean worker finishes the send in flight, records
   its receipt and starts nothing new (the release-97 worker cannot, so it is stopped only when idle right then); then the idle check — busy means the worker is started again and nothing changes;
4. backup to `backup/deploy-<ts>/`: an online SQLite copy that passes `quick_check`, clones of media, review payloads
   and release archives, `config/live`, the current config dir, `current.json`, `launch.py`;
5. stop the api (by port, wait until 8811 is free); record the reshoot cutoff (`studio/reshoot_cutoff.py`: only a
   blank 再拍一批 answered after this event re-fires by itself; if it cannot be written the running release starts
   again unchanged); `current.json` → the new code dir and config (the old one is
   kept as `current.previous.json`); install its launcher; start; health: `/health` answers `{"status":"ok"}`,
   `/review` answers 200 with the configured origin's Host, the worker process is up;
6. unhealthy → the previous code dir and config are put back and restarted; the outcome is in `evidence/deploy-<ts>.json`;
7. healthy → the providers' usage records are pulled for the 账本 (a failure there is reported, never a put-back).

`--local-desk` also moves the desk to `http://localhost:8811/review` with no login (`local_owner`, config dir
`lean-<sha>.config-local`); later releases copy that config and keep it.

`ops.sh restart` and `rollback` stop the worker the same way (SIGTERM, wait) before the api. Never `kill -9` the worker:
a paid request's id would be lost.

Rehearse a commit before deploying it (below). Deploy between shooting sessions: a deploy waits for idle.

## Rollback

`studio/ops.sh rollback` swaps `current.json` with `current.previous.json` and restarts. The database is never
restored as part of a rollback: records written by the newer code stay, and the older code reads them. Rolling back
to `runtime/release-97` works because `launch.py` hands a `release-*` code dir to `launch-release.py`, which uses the
untouched env files and `config/live/*.json`. After such a rollback the owner's desk (release 97 shows only enabled
memberships) does not show projects made on the lean platform until the operator sets their memberships.

## Rehearsal (spends nothing)

```sh
"$MVGP_STUDIO/venv/bin/python" tools/rehearsal/rehearse.py prepare --studio "$MVGP_STUDIO" --code-dir runtime/lean-<sha>
"$MVGP_STUDIO/venv/bin/python" tools/rehearsal/loop.py all --studio "$MVGP_STUDIO"
```

`prepare` restores the latest backup into `rehearsal/state` (an earlier one is moved to `rehearsal/old/`), writes the
lean configs for the copy, a rehearsal owner identity (key and JWKS under `rehearsal/owner`) and a self-signed
certificate; the API runs on `https://127.0.0.1:8812`. The loop builds SIMTEST projects through the agent CLI, shoots
through fake fal and apilio (every body fal receives is appended to `rehearsal/fal-requests.jsonl`), and plays the
owner on the real desk in headless Chrome. Evidence: `evidence/rehearsal-loop.json`. Everything stays under
`rehearsal/`; the tools refuse any other database.

## Backup and restore

The deploy makes a backup every time. For an extra one, the operations CLI:

```sh
cd "$(python3 -c 'import json,sys;print(json.load(open(sys.argv[1]))["code_dir"])' "$MVGP_STUDIO/current.json")"
"$MVGP_STUDIO/venv/bin/python" -m production.operations --config "$MVGP_STUDIO/config/live/operator.json" backup --input backup.json
```

`backup.json`: a new `destination`, the `media_root`, optional `max_total_bytes`/`max_files`. `restore` takes the
backup `source`, a **new** `destination` and the backup's `manifest_sha256`; it never writes over a live database and
revokes every credential in the restored copy. Old backups (with release archives) still restore.

## Money: holds and unknown outcomes

- A take's cost is reserved when it is submitted, frozen in its dispatch intent and checked again at dispatch; a price
  change in `runtime.json` refuses queued work instead of re-pricing it.
- A lost answer after sending is an **unknown outcome**: the hold stays, nothing is resent, nothing is closed as
  absent by fal or apilio (neither can list jobs). A lost Higgsfield create is settled by the worker itself from
  Higgsfield's job listing (adopted, or closed as absent once the listing proves it). The
  operator settles the others:
  - `reconcile-cost` with the provider's own bill entry (fal usage, apilio log) — the only way to turn a hold into spend;
  - `abandon-generation` / `abandon-observation` for an inactive unknown that cannot be looked up, or whose status
    reads are used up (20; the worker stops polling it) — the charge stays recorded for `reconcile-cost`;
  - `retry-result-download` when the provider finished but the download failed.
- Higgsfield holds stay reserved until settled against the provider's bill (`higgsfield account transactions`).
  New films get their own `hf_owner` envelope from `default_envelopes` in the API config.

All take an `--input` JSON file with an `idempotency_key`; repeat the same request to get its first answer back.

## Credentials

`issue`, `rotate` and `revoke` (operations CLI) make and withdraw agent, worker and viewer tokens, written to the
operator's private credential directory. The owner never needs a token: on this Mac the desk page itself gets his session (`local_owner`); behind the public
address Cloudflare Access would sign him in. `set-envelope`
changes a project's spending envelope; new projects get the `default_envelopes` from the API config.
