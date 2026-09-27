# The HF project folder

The agent works in one folder per film, shaped like a Higgsfield project, and syncs it with the platform through a
few commands. The platform keeps its own records; the folder is the writer's working copy of them.

Where: `<your films folder>/<film name>/`. The manuals are fetched outside it
(`<your films folder>/.manuals/<version>/`, `cli playbook`): they are shared tools, not part of a film.

Why this shape: HF's writer "held the whole project folder in its context: the script, the asset sheets, the
registry, the shotlists with @-tagged assets" (hell-grind:61). Project root = an assets area plus scene folders
(9 of 12, `production/HF_CANONICAL.md` §1b); kok-boru:23 files the work "by Acts, Scenes and Shot".

```
<片名>/
  brief.md                      the brief; `project: <id>` on its first line once `open` created the project
  script.md                     the screenplay
  registry.md                   looks per world + one section per asset (descriptor), the constants
  ASSETS/CHARACTERS/<tag>.md    the image prompt (LIRA) of that asset; <tag>.png beside it once made
  ASSETS/LOCATIONS/<tag>.md
  ASSETS/PROPS/<tag>.md
  SCENE 01 - KITCHEN/shotlist.md   one shotlist per scene block
  SCENE 02 - ROAD/shotlist.md
  log.md                        written by the platform (`pull`); never edited by hand
```

## brief.md

HF's brief sections (HF_CANONICAL §7): Logline (7 of 12) · About (10) · Tools (8) · Pre-production (10) ·
Production (8) · Post-production (9) · Conclusion (6). Free Markdown under those headings.

## registry.md

The constants of the film: "Descriptors and the fixed look-and-camera block of each world live as constants, so
one edit updates every shot at once" (cully:133; hell-grind:103 "The descriptors and the Style Prefix live as
constants"). "An asset is a pair: text + image. The text descriptor goes into every prompt word for word"
(red-flag:14; 7 of 12). One tag per asset, the same tag everywhere (hell-grind:59); a state is its own tag,
`@kel_wet` (6 of 12).

```
## Looks
### city
Photoreal night, sodium street light, fine grain …
### dream
Soft pastel, hazy bloom …

## @kel · character
late-20s, thin, messy dark hair, stubble, grey hoodie …

## @kel_wet · character
the same man soaked by rain, hair flat on his forehead …

## @loc_kitchen · location
a narrow night kitchen, one warm bulb …
```

Kinds: `character`, `location`, `prop` (HF's element categories, §2). A look's name is its world; a shot names it
(`look: dream`) when the film has more than one.

## ASSETS/<KIND>/<tag>.md

The writer's LIRA image prompt for that asset's one image (every HF element has exactly one image, §2), plain
prose; `image <tag>` makes it and puts `<tag>.png` beside it. A `.png` placed there by hand is uploaded as is. A
`<tag>.mp4` or `<tag>.mov` placed instead is the asset's reference video (a previs or a turnaround; passport-rush:24,
:54-57), sent to Higgsfield as a video reference and numbered HF's way, `<<<video_1>>>` (images `<<<image_1>>>`); one
picture or one video per asset.

## SCENE NN - NAME/shotlist.md

"Each block lives in its own shotlist file. Every shot has its number, timing and full prompt" (hell-grind:103;
cully:133). One section per shot:

```
## 010A · 8s · 蓝色送货车从红色路标左边开到右边
look: city

<the whole prompt, every block written by the writer, up to the next ## >
```

A recreation shot adds `source: <id>` (before or after `look:`): the `source-understanding` record of the source
shot it recreates, written after the reader watched the source (`observe`); `push` lists it in the card's
dependencies, which the platform needs while 复刻先让 Gemini 看原片 is on.

The number is free text without spaces, up to 16 characters of letters, digits, `.`, `_` and `-` (HF numbers shots `1.10`, `73.2B`, §1a); the scene's number and the shot's make the
label `S01-010A`. Seconds are whole (Higgsfield and fal take 4–30). The goal is one line in plain Chinese; it is what the owner
reads on the desk ("the goal of the shot in one line", cully:38). The prompt goes out verbatim plus only the
platform's five additions (`production/playbooks/writer.md` §四).

## log.md

Written by `pull` from the platform: per shot, version · what changed · takes · verdict · the owner's reason and
notes (cully:134: "version, what changed, verdict — 137 entries"; hell-grind:106). Picks (and their 1080p 正片) are downloaded into `TAKES/`; `--all` downloads every take.

## How the folder maps onto the platform

| Folder unit | Platform record |
|---|---|
| brief.md | project brief |
| script.md | `script` artifact |
| registry.md `## Looks ### <world>` | a `look` asset tagged `@look_<world>` |
| registry.md `## @tag · kind` + ASSETS/<KIND>/<tag>.md (+ .png) | an `asset` (role visual/world by kind, category) with `descriptor` and the image definition |
| SCENE NN - NAME/shotlist.md | a `scene` artifact (its name and shot list) |
| each `## <number> · <s>s · <goal>` | a `shot` card: `_production.prompt`, `look`, the seconds, the goal |
| log.md | read from the project tree's version log |
