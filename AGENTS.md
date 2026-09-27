# MVGP agent entry manual

MVGP is a standalone film production platform. Read this first, then the manual for your task.

## Manuals

- [AGENT_GUIDE.md](production/AGENT_GUIDE.md): agent commands, the shot workflow and what blocks a take.
- [FOLDER.md](production/FOLDER.md): the film folder and filesystem CLI.
- [OPERATIONS.md](production/OPERATIONS.md): credentials, services, releases, backups and money reconciliation.
- [HF_CANONICAL.md](production/HF_CANONICAL.md): evidence for Higgsfield practice. Follow it; do not invent rules.
- [writer.md](production/playbooks/writer.md): the writer's instructions and references to the bundled
  CINEDANCE, image-prompt and acting manuals in `production/playbooks/`.
- [README.md](README.md): first install, provider setup and no-spend rehearsal.

## Film agents

1. Fetch the current manuals and read the scene, assets and shot cards before writing.
2. Follow the HF canonical practice and writer manual. Keep the writer's prompt and reference numbering intact.
3. Before **every order**, the writer runs a fresh reviewer with the same manuals, scene and shotlist. Findings
   must cite a manual line. Patch the findings and supply the review notes with the order.
4. Quote before paid generation and get the human's yes. A spending envelope, provider key or successful
   rehearsal is not consent to spend. Never invent approval or a provider receipt.
5. Only money, provider limits, send integrity and owner switches block work. Craft advice is advice. Respect
   the enabled stress-test and source-understanding switches; do not create new taste gates.
6. The human watches and selects takes on the desk. Never impersonate the human or make their picks.

Use IDs, revisions and digests returned by the platform. Follow the manuals when a result is unknown; do not
resend a paid request to guess whether the first one worked.

## Coding agents

Read affected code and tests before editing. Preserve routes, prices, money gates, provider boundaries and
shooting behavior unless the task explicitly authorizes a change. Keep secrets and film assets out of Git;
never print env files, credentials, token files or the Higgsfield sign-in. Keep `.claude/`, `experience/`, venvs
and Python caches ignored. Use a task-specific temporary studio for tests; never write to another studio.

Run the full suite from the repo root with Python 3.12 and `production/requirements.txt` installed:

```sh
.venv-production/bin/python -m playwright install chromium
.venv-production/bin/python -m unittest -q \
  $(ls production/tests/test_*.py studio/tests/test_*.py tools/tests/test_rehearsal_*.py | sed 's#/#.#g; s#\.py$##')
```

The studio's `venv/bin/python` can replace `.venv-production/bin/python`. Tests need ffmpeg/ffprobe and local
listeners; rehearsal also needs `cc` and `openssl`. Report exact test results and any unverified behavior.

## Releases

1. Test the candidate commit and rehearse it from a clean checkout: `rehearse.py prepare`, then `loop.py all`,
   using the README commands. Check the evidence and explain any skips.
2. Deploy the commit with `studio/deploy.sh <commit> --local-desk` to the intended `MVGP_STUDIO`. The deploy
   validates config, drains work, backs up, switches code/config, starts services and checks health.
3. Check `studio/ops.sh status`, `/health`, `/review` (200), and `current.json`. Keep deploy evidence.
4. Roll back code/config with `studio/deploy.sh --rollback` if needed; never restore a database over live state.

Use SIGTERM for workers so in-flight sends can finish. Do not force-kill them or push to a remote unless the
user authorized it. Obtain independent verification before a push or PR.
