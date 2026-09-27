# MVGP

MVGP 是一个给 AI 智能体用的拍片平台。你只管跟智能体聊想拍什么，剩下的写剧本、拆镜头、下单生成、记账，
都由智能体按规范在平台上完成。片子出来后，你上工作台看片、选片，就结束了。

MVGP is an agent-first film production platform. You tell an AI agent what you want to film; the agent does the
rest on the platform by the book: script, shot breakdown, generation orders and accounting. When the takes are
in, you watch and pick them on the review desk, and you are done.

How a film moves through it:

1. **Talk.** Tell the agent what to film: an original story or a recreation of an existing scene.
2. **The agent works by the book.** It writes the script, the shot breakdown and each shot's prompt following
   the practice of Higgsfield's official projects. A fresh reviewer checks every shot before it is ordered, and
   anything that costs money is quoted first and waits for your yes.
3. **The platform generates.** Video goes to Higgsfield at 1080p by default; the sample mode (样片模式) uses fal.
   The platform keeps the ledger and holds each film to its budget.
4. **You watch and pick.** The desk shows the takes of every shot; pick the one you want, or send a shot back to
   be shot again.

The desk runs locally on your Mac, with no login. Film and coding agents start at [AGENTS.md](AGENTS.md).

## Requirements

- macOS, Python 3.12, Git, ffmpeg and ffprobe on `PATH`.
- For tests: Playwright Chromium (`python -m playwright install chromium`). Rehearsal also needs the macOS
  command-line developer tools (`cc`) and `openssl`.
- For the default shooting route: the Higgsfield CLI and a signed-in account. Use the **native executable**
  shipped with `@higgsfield/cli` (`vendor/hf`), not its Node launcher.
- `FAL_AI_TOKEN` and `APILIO_AI_KEY` are optional for opening the desk and rehearsal; the corresponding live
  routes need them. The source reader also uses apilio. Ledger reconciliation optionally uses `FAL_AI_ADMIN_TOKEN`.

## Install and open the desk

```sh
git clone https://github.com/0xmariowu/MVGP.git
cd MVGP
export MVGP_STUDIO="/Users/Shared/mvgp-studio"
export MVGP_PORT=8811
# Optional: point at your installed native Higgsfield executable before init.
# export MVGP_HF_BIN="/absolute/path/to/vendor/hf"
studio/init.sh
"$MVGP_STUDIO/venv/bin/python" -m playwright install chromium
```

Init creates a venv from `production/requirements.txt`, private sample configs, an empty store, service launchers,
and owner-agent/worker/film credentials through the operations CLI. To reuse an installed environment, set
`MVGP_PYTHON=/absolute/path/to/venv/bin/python`; it is linked as the studio's venv and left unchanged. Otherwise
`MVGP_PYTHON` can select a Python 3.12 interpreter. An existing `$MVGP_STUDIO/venv` is also reused.
Repeating init preserves every file. A partial or foreign install is refused if files would be overwritten.

Fill `env/live-worker.json` with the provider keys you use; leave unused provider keys empty. Init fills the two
service tokens and the port for you. `env/live-ledger.json` holds the optional fal admin key. Keep these files
mode **0600** and never commit them or print their contents.

With `MVGP_HF_BIN` set, init copies and pins that native binary as `bin/hf` and configures its private `hf-home`.
Follow the CLI's sign-in instructions with `HOME="$MVGP_STUDIO/hf-home"` and that binary; use
`HOME="$MVGP_STUDIO/hf-home" "$MVGP_STUDIO/bin/hf" --help` to see its commands. If you initialized without it,
the desk and rehearsal work, but live Higgsfield shooting needs an `hf` section in `config/live/worker.json`
before the first deploy (see `HFConfiguration` in `production/worker.py`: native path, SHA-256, exact `--version`
output, credential home, service UID, and `{"seedance_2_5":"hf_video_capability"}` capability roles).

Review `config/live/api.json` before shooting. Its sample per-project envelopes are 8,000 HF credits,
270,000,000 USD micro-units for fal, and 5,000,000 apilio quota units. These are configurable limits, not quotes
or permission to spend. The agent must quote and get your yes before paid work.

```sh
studio/deploy.sh HEAD --local-desk
open "http://localhost:$MVGP_PORT/review"
```

Deploy archives the commit into `runtime/lean-<sha>`, validates configs, backs up, starts API and worker, and
checks health. Services run from that deployed directory. `current.json` records the active code and configs;
after deployment, edit or prepare release configs using [OPERATIONS.md](production/OPERATIONS.md), not stale
`config/live` copies. Keep `MVGP_STUDIO` and a custom `MVGP_PORT` exported for later operations.

The owner's agent credential is `config/live/credentials/owner-agent.token`. For the agent CLI:

```sh
export MVGP_URL="http://localhost:$MVGP_PORT"
export MVGP_TOKEN="$(cat "$MVGP_STUDIO/config/live/credentials/owner-agent.token")"
"$MVGP_STUDIO/venv/bin/python" -m production.cli --help
```

Credentials expire after 90 days; use the operations CLI's `rotate` command before expiry and update the service
token values in `env/live-worker.json`. See [credentials and operations](production/OPERATIONS.md).

## Rehearse and release

From the checkout containing the candidate commit:

```sh
PY="$MVGP_STUDIO/venv/bin/python"
"$PY" tools/rehearsal/rehearse.py prepare --studio "$MVGP_STUDIO" --code-dir "$PWD"
"$PY" tools/rehearsal/loop.py all --studio "$MVGP_STUDIO"
studio/deploy.sh HEAD --local-desk
studio/ops.sh status
curl -s -o /dev/null -w '%{http_code}\n' "http://localhost:$MVGP_PORT/review"
```

Use a clean checkout of the commit you will deploy. Rehearsal uses the latest backup when available, or an empty
store and the init configs on a fresh studio. It creates its own credentials and fake providers; it spends
nothing and never uses the live provider keys or Higgsfield sign-in. Its default port is 8812; set
`MVGP_REHEARSAL_PORT` consistently for prepare and loop to use another free port. The loop stops its own services;
`rehearse.py down --studio "$MVGP_STUDIO"` also stops an interrupted run.

Evidence is in `evidence/rehearsal-loop.json` and `evidence/deploy-*.json`. On an empty studio, the check that old
reshoot answers do not create new batches is explicitly skipped: there are no historical batches to test.
The remaining checks create their own films. Deploy failures restore the prior code/config; the first install
has only its bootstrap configs to fall back to. To roll back a later release: `studio/deploy.sh --rollback`.
No database rollback is performed.

`studio/ops.sh restart` restarts the current services. The installed `$MVGP_STUDIO/ops.sh` also supports restart,
status, rollback and ledger; set `MVGP_REPO` to the source checkout when using its `deploy` command.
`run-service.sh api|worker live` is the entry point for your own launchd setup; init does not install launchd jobs.

## Tests

From the repo root, using the studio venv (or `.venv-production` with the same requirements):

```sh
"$MVGP_STUDIO/venv/bin/python" -m unittest -q \
  $(ls production/tests/test_*.py studio/tests/test_*.py tools/tests/test_rehearsal_*.py | sed 's#/#.#g; s#\.py$##')
```

The suite includes real browser and local socket tests. Run it on a machine that allows local listeners.

## Repository map

- `production/`: API, worker, agent CLI, review desk, runtime config, manuals and tests.
- `production/playbooks/`: writer and Higgsfield practice manuals.
- `studio/`: init, deploy, launcher, operator tools and tests.
- `tools/rehearsal/`: isolated no-spend integration loop and fake providers.
- `production/templates/`: shot card template.

Films, credentials, state and media belong in your studio, outside the repository.

## License

[MIT](LICENSE), Copyright (c) 2026 0xmariowu. The bundled IBM Plex fonts retain their
[OFL license](production/web/fonts/OFL.txt).
