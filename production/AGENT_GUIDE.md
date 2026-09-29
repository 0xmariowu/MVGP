# Use MVGP from a coding agent

The owner talks to you, then watches, picks and judges takes on the platform's review desk (`/review`). That is his
whole part. You do everything else the way Higgsfield's teams did. You write in an HF-shaped folder; six commands
sync it with the platform. The platform keeps the records, sends each shot to Seedance 2.5 on Higgsfield as four
1080p takes (a film in 样片模式 goes to fal instead: four 480p drafts, the owner's pick completed to 1080p), shows the
project like an HF project page and records his picks. It does not review your work, approve your plans or watch your takes.

## Setup

The operator gives you `MVGP_URL` and a scoped `MVGP_TOKEN` in your private environment (plus `MVGP_ACCESS_ORIGIN`,
`MVGP_ACCESS_CLIENT_ID`, `MVGP_ACCESS_CLIENT_SECRET` behind Cloudflare Access: all three or none). Never put a token in
a URL, prompt, argument, screenshot or file. A person's Access login is never an agent credential; the CLI refuses
`MVGP_ACCESS_TOKEN`. Below, `mvgp` means `.venv-production/bin/python -m production.cli`, run from the platform checkout.

Output is JSON on stdout; errors are JSON on stderr. Exit codes: 2 input, 3 permission, 4 not found, 5 version or
idempotency conflict, 6 other platform refusal, 7 transport, 8 polling timeout, 9 invalid response. Report a refusal
with its details; never change a rule to make a request pass.

## The folder

One folder per film at `<your films folder>/<film name>/`, laid out in `production/FOLDER.md`: `brief.md`, `script.md`,
`registry.md` (looks and asset descriptors, the constants), `ASSETS/<KIND>/<tag>.md` (image prompts, `.png` beside),
`SCENE NN - NAME/shotlist.md` (one `## <number> · <s>s · <goal>` section per shot, the whole prompt under it), and
`log.md` (written by `pull`). HF's writer "held the whole project folder in its context" (hell-grind:61).

## The commands

| Command | What it does |
|---|---|
| `mvgp open <dir>` | Creates the project from `brief.md` (or resumes it), fetches the manuals to `<films>/.manuals/<version>/`, makes the skeleton |
| `mvgp push <dir>` | Sends the units that changed since the last push, nothing else (the other commands push first) |
| `mvgp image <dir> <@tag…>` | Makes each asset's image from its `.md` and puts the `.png` beside it |
| `mvgp quote <dir> [<shot…>]` | What shooting those shots (default: all) would cost; spends nothing |
| `mvgp shoot <dir> <shot…> --review <file>` | Orders four takes of each shot, with the reviewer's notes |
| `mvgp pull <dir> [--all]` | Writes `log.md` and downloads the picks (every take with `--all`) into `TAKES/` |

A shot is `S01-010`, `01:010`, or a scene number (`01`, every shot in it). Running a command again repeats nothing:
an unchanged unit is not sent, an image order still out is waited for, and the same card versions find the same
shoot order. State lives in `<dir>/.mvgp/state.json`; do not edit it.

## Making a film

1. **Open and read the manuals.** `mvgp open <dir>` writes `writer.md`, CINEDANCE, ACTING and LIRA and records that
   your credential took that version; the desk marks 手册不是最新 when it has not. Read them before you write a prompt (owner
   2026-09-24: "别嘴上说交了，结果没交出去。结果还是自己发挥").
2. **Brief and script.** HF's brief sections: Logline, About, Tools, Pre-production, Production, Post-production,
   Conclusion (`production/HF_CANONICAL.md` §7). Write what the audience must see and understand in each scene before
   any prompt.
3. **Assets.** An asset is a descriptor plus one image (7 of 12; red-flag:14). Put the descriptor (who or what it is,
   the wardrobe; one or two sentences) in `registry.md`: it is pasted word for word into prompts. Each state is its
   own tag, `@kel_wet` (6 of 12, half; an asset holds one image). Write the image prompt in `ASSETS/<KIND>/<tag>.md` as LIRA prose: natural
   sentences, no label blocks, no settings the route already sets (`lira-image-prompts.md:159-164`); a character is
   usually one grey 16:9 sheet with front and back full body and a large 3/4 portrait. Then `mvgp image <dir> @kel`.
   Images go through apilio at 16:9, 2048×1152 (owner 2026-09-24). A `.png` you place by hand is uploaded as is.
   A `.mp4` or `.mov` placed instead (a Blender previs, a 360° turnaround with expressions) is the asset's reference
   video: on Higgsfield it goes as a video reference, numbered HF's way, `<<<video_1>>>`, apart from the images
   (`<<<image_1>>>`…; fal's `@Image1` only in 样片模式); say in the prompt what it is for, e.g. `@previs — motion,
   camera and timing reference only; the grey-box look is not inherited`
   (passport-rush:24, :54-57; 70% of its Seedance 2.5 jobs). 样片模式 (fal) refuses a video reference.
4. **Looks.** One look per world under `## Looks` in `registry.md`. A shot names it (`look: city`) when the film has
   more than one.
5. **Shots.** Write the whole prompt yourself under each shot header (writer.md; `cully-hill-boys.txt:25`).
   CINEDANCE's blocks are the default form; leave out what the shot does not need. Paste each descriptor and adapt it
   to the shot; acting is one paragraph per person led by the @tag. The platform sends your text verbatim and adds
   only what you left out: the reference number before a tag's first mention (`<<<image_1>>>` on Higgsfield, HF's own
   token; `@Image1` on fal in 样片模式), the descriptor of a tag you never described, the look, the
   duration, "No music." (writer.md §四). The goal after the seconds is one plain Chinese line: the owner reads it
   on the desk. Run CINEDANCE's 20-question audit silently; never write the checklist into the prompt
   (`cinedance-v4-seedance.md:149`).
6. **Recreation.** Read the source first: `mvgp observe` the source clip (the reader lane; it spends the project's
   reader budget, so keep each clip to a scene), `mvgp draft` one `source-understanding` per shot citing that
   observation with the source shot's `start_seconds`/`end_seconds`, and put its id on the shot as `source: <id>`
   (production/FOLDER.md). The desk plays that source segment next to the takes.
7. **A fresh reviewer checks the prompts before shooting.** Hand the scene's script and its shotlist to a new agent
   that did not write them (HF: the auditor "re-checks every prompt before it goes out", `cully-hill-boys.txt:25`;
   owner 2026-09-26). It reads the same manuals and checks against them only: a finding names the manual line it
   breaks; anything no line covers is not a finding. Fix every finding by patching only that section ("patches only
   the section that failed, because a fully rewritten prompt loses the parts that already worked", cully:25). Where
   you read a line differently, follow the reviewer. Answer each note with the section you patched, in a JSON file:

   ```json
   {"reviewer": "fresh reviewer", "notes": [
     {"line": "cinedance:336", "note": "No lens named.", "answer": "CAMERA: added 35mm.", "shot": "S01-010"}]}
   ```

   The owner is not asked. A shot shot without a review shows 没审 on the desk.
8. **Quote, then shoot.** `mvgp quote <dir> 01` prints each shot's cost (and, in 样片模式, the 1080p completion of one
   pick) and what is left in the envelope. Then `mvgp shoot <dir> 01 --review review.json`. The platform prepares, fires, waits
   and puts the takes on the desk. By default each take is Seedance 2.5 on Higgsfield at 1080p, 12 credits a second
   (a 4 s take 48 credits; `higgsfield generate cost`, 2026-09-27), with up to 30 reference images; photoreal people
   are fine there. A film whose owner turned on 样片模式 (个人设置) goes to fal instead: each take a 480p draft, the same
   frames and seed as its 1080p version, about a fifth of the price (a 4 s draft US$0.83, its completion US$4.60;
   2026-09-25 probe); fal refuses photoreal faces in references. Both take 4–30 whole seconds. A shot with the line
   `resolution: 720p` (production/FOLDER.md) shoots Higgsfield at 720p, 7 credits a second (owner 2026-09-29).
9. **Read what the owner said.** `mvgp pull <dir>` writes `log.md`: per shot, each version, what changed, the takes,
   the verdict and his reason, the reviewer's notes, and his notes by take and second. The picks land in `TAKES/`.

## Reshoots

- He picks a take. A Higgsfield take is already 1080p. A fal draft (样片模式): ten minutes later the platform
  completes it to 1080p by itself (once per draft; a failed
  completion that cost nothing is retried once). A pick changed within the ten minutes costs nothing. A draft can be
  completed for seven days; after that the film uses the 480p draft. You never order a completion.
- 再拍一批 with no reason: the platform shoots the same card again, four new takes, by itself.
- 再拍一批 or 都不行 with a reason: change the smallest thing that answers it, one section, and push. The shot's
  version note names the fields you changed; the desk shows 这版改了什么. Then `mvgp shoot` again.
- The latest human record on a shot wins. An undo takes back his 再拍一批 / 都不行 while the card is unchanged; a later
  pick, even from an earlier batch, replaces a reshoot order. Earlier batches stay pickable after you change the card.
- After 10–15 versions, simplify the shot (split it, less action, another angle), not the wording (hell-grind:106).
- Picks play in order as the film (看成片). If he changes a pick after 这版可以, the platform reopens that shot and
  re-cuts the film.

## Asset stress test

Off by default; the owner turns it on in 个人设置. HF's test is a video test: "Ten generations — different actions,
different shot sizes, different locations — recognizable in ten out of ten" (`cully-hill-boys.txt:62-65`). Test the
character with the location and the assets it shares the frame with. When it falls apart, fix the words first; if
the same thing breaks again, rebuild the asset. While the switch is on, every identity image needs its 10 stress
takes before a narrative take.

## What blocks a take

Only money (the envelope), the provider's limits (route, reference count, resolution, aspect, 4–30 s), the integrity
of what is sent (a changed card after preparation, an altered candidate, a picture lock), the stress test when it is on, and a recreation shot's source-understanding while 复刻先让 Gemini 看原片 is on.
Everything else comes back as advice. There is no AI review, no AI director and no plan approval.

## HF practice

`production/HF_CANONICAL.md` §13 counts every practice across HF's 12 official projects. The majority is your default; the
minority is a tool you may use when the shot needs it. Nothing checks either.

- Majority (of HF's 12 official projects; production/HF_CANONICAL.md): English prompts (9 of 12), long (11),
  "no music" (10), the lens stated (9), the duration stated (8), references by tag or image position (12), four
  takes per batch (10), audio on (12), assets area plus scene folders (9), several cuts inside one generation (7; owner agreed); write every
  cut in ACTION TIMING.
- Minority: timed beats (6 of 12, half); naming `type_CODE_name[_sN][_vN]` (5; cully:57-58); voice as fixed text pasted verbatim each time a
  person speaks ("Voice is not an asset", cully:67); behaviour and tactics, not emotion words (cully:109-113); one
  acting paragraph per person (`acting-system.md:158`); Cully's four-group shot card (cully:36-39); the 15-block
  CINEDANCE skeleton (3 of 12), empty blocks left out (`cinedance-v4-seedance.md:157-176`).
- When a take fails, change one block, not the whole prompt, and say what changed (cully:134, hell-grind:106).

## Money and keys

A new project is funded and on the owner's desk when `open` creates it. Spend is recorded per project; never ask the
owner to approve a budget. When an envelope runs out, the stop reason says so and the operator raises that project.
Keep every idempotency key stable: a repeated request replays; a job with an unknown paid outcome is never a reason
to submit another. The operator reconciles.

## Lower-level commands

`mvgp discovery` lists every route and its schema. `mvgp --help` lists the commands: `read`, `history`, `context`,
`observe`, `draft`, `revise`, `patch`, `candidate-inspect`, `status`, `upload`, `request-decision`, `resolve`. Use
them for what the folder does not cover (recreation sources, a copied frame reference from the desk). Take exact
object ids, revisions and digests from responses; never invent one. The owner's picks and notes come only from his
own session; this CLI has no command to confirm for him.
