<!-- Census of the 12 official Higgsfield projects (every ledger line), built 2026-09-24 for 11 and extended 2026-09-27 to Passport Rush. Counts come from a private mirror of the HF projects, which is not distributed. The census scripts are not included. This is the spec MVGP and its agents follow. -->
# HF canonical project spec: what the 12 official Higgsfield projects actually do

Generated 2026-09-24; extended 2026-09-27 to the twelfth official project, Passport Rush (`passport-rush:line` = `briefs-clean/passport-rush.txt`, decoded with the yjs decoder). Read-only on sources. Scope (owner 2026-09-24, confirmed 2026-09-27 "官方 12 部，社区单列"): **the 12 official projects define the spec**, with counts given as "N of 12". A practice is a majority when more than half of the projects follow it (7 of 12 or more); 6 of 12 is half, not a majority. The 6 community projects get one summary row each (§10) and are left out of every count; where a number helps, their count is given apart as "community N of 6".

## 0. Sources, method, and evidence tiers

The counts below come from a private mirror of the HF projects, which is not distributed.
Source paths below are relative to that mirror; derived evidence and census scripts are not included.

| Code | Source | Notes |
|---|---|---|
| L | `hf-projects/{official,community}/<slug>/folders.jsonl` + `items/*.jsonl` | Every line was parsed. No sampling, including Cully (474,105 placements) and Hell Grind (115,571 placements). |
| DB | `scratchpad/hfspec/work.db` (built by `extract.py`) | Holds one row per placement, deduplicated prompts, reference elements and media roles. Derived numbers are in `analysis.txt` (settings, review fields), `analysis2.txt` (folder trees, elements), `analysis3.txt` (prompt structure), `analysis4.txt` (asset folders), `analysis5.txt` (prompt phrases, uuid check), and `prompt_samples.txt` (10 random video prompts per project, seed 42). |
| B | `higgsfield-originals/briefs-clean/<slug>.txt:line` | Cited as `slug:line`. **Caveat:** `briefs-clean/kok-boru.txt` is the **kok-boru-film** brief. Its text and line numbers match the decoded `kok-boru-film` brief.bin, while the decoded `kok-boru` brief.bin is a different, trailer brief. |
| BD | `scratchpad/hfspec/briefs/<slug>.txt:line` | Decoded locally from `brief.bin` with y_py (`decode_briefs.py`). Used for **trigger**, **kok-boru (trailer)** and all 6 community projects, because briefs-clean has no copy of these. Cited as `slug(BD):line`. |
| S | `higgsfield-originals/skills/cinedance-v4-seedance.md:line`, `attachments/CHECKLIST.md` | The skill files and the attachment audit. |
| V | Sheets viewed with the Read tool: `attachments/assets/cully-hill-boys/char_CB_Kel_v3.jpg`, `hell-grind/JAX.png`, `oneiric/char_ON_Rudy_s2_v1.png`, `red-flag/char_RF_Lee_wet.webp`, `adiliada/ADIL.jpg` | 5 images were looked at. No other images were viewed. |

Counting rules:
- "Video job" means a distinct `job.id` whose `job_set_type` returns video (20 model types; `analysis.txt:51`).
- A **job-set size** is the number of distinct mirrored jobs that share one `job_set_id`, which approximates takes per batch. The `batch_size` param is empty on almost every video job (`analysis.txt`).
- Prompt statistics cover **all distinct video prompts** per project. The 10-prompt samples exist only so a person can read examples.
- Header detection is a regex for the header at the start of a line, in upper case (`analysis3.py`). It **undercounts** headers written inline or in a different style. Treat header percentages as lower bounds.

---

## 1. Folder skeleton

### 1a. Per project (official). Full trees are in `analysis2.txt:1-1155`.

| Project | Top level (from `folders.jsonl`) | Second level / pattern | Categories present |
|---|---|---|---|
| adiliada | 4 part folders: `1. COLD OPEN: "The Crow Hunt"`, `2. MAIN TITLE SEQUENCE`, `3. SPACE SCENES`, `4. MEETING THE VILIAN` | Numbered shots such as `1.1 Window Takeoff` … `1.10 "Again"`, each holding a `Generations` subfolder, plus `ASSETS` and `LOCATIONS` inside each part (e.g. `ASSETS / Adil Hunter`, `LOCATIONS / Cathedral`) | scene(part), shot, assets, locations, title |
| cully-hill-boys | `0 Regenerations`, `1 ACT (1-49)`, `2 ACT (50-93)`, `3 ACT (93-120)`, `4ACT_Epilogue (121-137)`, `PRE  PROD` | Acts hold scene folders `NN scene` / `NN-NN` / `title scene`. Inside those are `prg_vN` (52× `prg_v1`), `shot N`, `assets`, `test`, `peregen`. `PRE  PROD` holds `ASSETS` (named characters, `LOCATIONS`, `PROPS`, `COSTUMES`, `SIDE CHAR`, `EDITS`), `Character Test`, `CLEAN UP`, `Costumes`, `Test_v1…v5`, `Test_scene16` | act, scene, shot, assets, characters, locations, props, costume, test, regenerations, title, edit |
| hell-grind | 108 top folders: `1. COLD OPEN`, `Assets`, `Credits`, `Flashbacks`, `Regenerations`, `Series`, then about 100 flat `Scene NN` folders, e.g. `Scene 27.1`, `Scene 73.2B`, `Scene 72: Roko vs Dagon`, with duplicate names such as three `Scene 67` | `Assets` holds 14 folders grouped by scene or block (`Assets Pizzeria`, `Assets Tibet Sanctum`, `Asset_Soroh`, `Roko's Shield Asset`). `Regenerations` holds 14. `Credits` holds 5 `End Credits …` | scene, assets (by scene), regenerations, credits, flashbacks |
| if-you-stop-loving-me-ill-die | `Development`, `PT-1`, `PT-2`, `PT-3` | `Development/{Animation_test, Characters_test, Locations_test, Props, Scenes_Test, Stills_test}`. `PT-1/{sc0 - title, sc1, sc2, sc3}` → shots `1-1 … 1-11`, `2-2 re-generation` | part, scene, shot, test, props, characters, locations, title, regenerations |
| kok-boru (trailer) | `SH01 … SH10`, `FULL10MIN` (empty), `Project Brief`, `Персонаэи` (empty; "characters", misspelled) | none | shot, characters (empty) |
| kok-boru-film | `ACT 1`, `ACT 2`, `ACT 3` | `SC01 … SC09`, `SC 10 … SC 15` | act, scene. **No assets folder.** |
| oneiric | `ASSETS`, `regenerations`, `SCENE 1 - KITCHEN` … `SCENE 12 - TITLE SEQUENCE` (plus `3B`, `9A`) | `ASSETS/{ANIMALS, CHARACTERS/{Main,Supporting}, EXTRAS, LOCATIONS, PROPS, TITLE SEQUENCE}`, each asset with a `test` subfolder. Scene folders hold `shot 1…8`, `shot 5A`, `transition s5-s6`, `VO Bob`, `Re-Gen` | assets, characters, locations, props, extras, animals, title, scene, shot, test, regenerations |
| red-flag | `1 scene`, `2 scene`, `3 scene`, `assets`, `test` | none | scene, assets, test |
| trigger | `ASSETS`, `SCENE 01 - KILLER'S WORK` … `SCENE 06 - WAR FLASHBACK`, including `SCENE 05 - CREDITS` | `ASSETS/{Characters/{JACK,TERRENCE,…}, Costume, Locations/{AFGHANISTAN,…}, Props}`. Some scenes hold beat subfolders (`GUEST CHECK`, `APACHE`) | assets, characters, costume, locations, props, scene, credits |
| zephyr | `Characters`, `Iterations`, `Production` | none | characters, iterations, production (selects) |
| zephyr-special | `regenerations` | none | regenerations only |
| passport-rush | `Art_direction/Assets`, `Seq1_Aeroport`, `Seq2_TaxiDriver`, `Seq3_Cop`, `Seq4_Ninja`, `Seq5_Home/Aeroport` (84 folders) | `Assets/{1_Characters/{Girl, Girl_test, Ninjas, Ninjas_test, Cat, Crawd, …}, 2_Locations, 3_Props, 4_Vid_Slop_Check}`. Sequences hold scene folders (`sc1 … sc18` in Taxi) or shot-range folders (`1-13`, `19-25`, `SH_`), plus `regen`, `regen 2`, `REGEN`, `ReGEN Alibek` | part (sequence), scene, shot range, assets, characters, locations, props, test, regenerations |

### 1b. Category counts (N of 12 official)

| Category | Projects | N of 12 |
|---|---|---|
| Dedicated asset area at any depth (ASSETS / assets / PRE PROD / Development / Characters / Art_direction) | adiliada, cully, hell-grind, if-you-stop, oneiric, red-flag, trigger, zephyr, passport-rush (+kok-boru, empty) | **9** (10 if the empty folder counts) |
| Top-level folder literally named assets/ASSETS/Assets | hell-grind, oneiric, red-flag, trigger | 4 |
| Asset subfolders split **by type** (characters / locations / props …) | adiliada (ASSETS + LOCATIONS), cully (PRE PROD/ASSETS), if-you-stop (Characters_/Locations_test, Props), oneiric, trigger, passport-rush (1_Characters / 2_Locations / 3_Props) | 6 |
| Scene folders | adiliada (parts), cully, hell-grind, if-you-stop, kok-boru-film, oneiric, red-flag, trigger, passport-rush (`sc1 … sc18` inside sequences) | **9** |
| Shot folders | adiliada, cully (partial), if-you-stop, kok-boru (SHnn), oneiric (scenes 1, 2, 4, 8), passport-rush (shot ranges `1-13`, `19-25`; "shot-range folders", `passport-rush:89`) | 6 |
| Act / part level above scenes | adiliada, cully, if-you-stop, kok-boru-film, passport-rush (five sequences, `passport-rush:63-69`) | 5 |
| Regenerations folder (regen / Re-Gen / peregen / re-generation) | cully (52 folders), hell-grind (6), if-you-stop, oneiric (2), zephyr-special, passport-rush (5; "we first assembled a full cut of the film, then regenerated individual scenes", `passport-rush:69`) | 6 |
| Test folder | cully, if-you-stop, oneiric, red-flag, passport-rush (`Girl_test`, `Ninjas_test`, `4_Vid_Slop_Check`) | 5 |
| Title / credits | adiliada, cully (`title scene`), hell-grind (`Credits`), if-you-stop (`sc0 - title`), oneiric (`SCENE 12 - TITLE SEQUENCE`), trigger (`SCENE 05 - CREDITS`) | 6 |
| Costume folder | cully, trigger (+hell-grind `Asset Home Clothes`) | 2–3 |
| Extras / animals folder | oneiric | 1 |
| A "selects / final / approved" folder | zephyr only: `Production` holds 275 jobs, and 266 of them are also filed in `Iterations` (`work.db`, multi-placement query) | 1 |

**Most-shared skeleton (majority practice):** a project root holding an **asset area plus scene folders** (9 of 12 each). Everything below that level varies. The fullest instance of the pattern is `ASSETS/{CHARACTERS, LOCATIONS, PROPS, …}` next to `SCENE N - NAME/shot N` (oneiric, trigger), which is 2 of 12 exactly and 6 of 12 when asset folders need only be split by type.

**Scene naming:** the word "scene" appears in the name in 5 of 12: `SCENE NN - PLACE` (oneiric, trigger), `Scene NN` (hell-grind), `NN scene` (cully, red-flag). An abbreviation is used in 3 of 12: `SCnn` (kok-boru-film), `scN` (if-you-stop) and `scN` (passport-rush). **Shot naming** has no majority form: `shot N` (oneiric, cully), `N-M` (if-you-stop), `N.M Title` (adiliada), `SHnn` (kok-boru), shot ranges `N-M` (passport-rush).

Brief evidence for the hierarchy: kok-boru-film says "organized by Acts, Scenes and Shot, with each folder holding the many iterations" (`kok-boru:23`, i.e. briefs-clean kok-boru.txt), and the trailer brief says "organized by shot" (`kok-boru(BD):22`). if-you-stop says "filed in folders the way it is numbered: part → scene → shot. The file count in a folder is a rough measure of a shot's iteration weight" (`if-you-stop:183-185`).

---

## 2. Elements (`params.reference_elements`)

Distinct elements are counted by element id (`analysis2.txt:1655-1830`).

| Project | Distinct elements | Category mix (char / env / prop, including `auto:` variants) | Naming convention | Images per element | Description filled |
|---|---|---|---|---|---|
| adiliada | 32 | 19 / 10 / 3 | Free names: `ADIL`, `Hunter_Adil`, `LOC_TOWNHOUSE`, `RAVEN_SCALE` | 1 (all) | 0% |
| cully-hill-boys | 1,391 | 444 / 628 / 319 | **type_CODE_name[_sN][_vN]** for 1,203 of 1,391 (86%): `char_CB_Kel_v9_1`, `loc_CB_…`, `prop_CB_ak47_s23-30-35-95-99-115`. Other prefixes: `poster_CB_`, `voice_CB_`, `blend_CB_`, `element_CB_` | 1 | 6 (0.4%) |
| hell-grind | 332 | 247 / 21 / 64 | Free names: `ROCO_COLD`, `REIN_LATE`, `roko_bloody`, `crystal_sword` (the brief's style `@roco`, `@roco_wet` is at `hell-grind:48`) | 1 | 9 (3%) |
| if-you-stop | 33 | 10 / 17 / 6 | **kebab type-code-name** for 29 of 33: `ch-kran-muzhik-dead`, `bg-kran-kitchen-fridge`, `prop-kran-eggs`, `ref-kran-pose-drag` (convention at `if-you-stop:57`) | 1 | 0% |
| kok-boru | 0 | n/a | No elements. Uses positional images (`<<<image_N>>>`, 95% of prompts) | n/a | n/a |
| kok-boru-film | 25 | 21 / 3 / 1 | Free, partly Cyrillic: `Офцы`, `Нож`, `мерген-2`, plus a `STYLE` element | 1 | 1 (4%) |
| oneiric | 127 | 62 / 46 / 19 | **type_CODE_name_sN_vN** for 123 of 127: `char_ON_Sam_s2_v1`, `loc_ON_dorm_commonroom_front_s2` (convention at `oneiric:33-35`) | 1 | 0% |
| red-flag | 51 | 11 / 19 / 21 | Mixed: 24 `type_RF_name`, 11 `type_name`, 16 other (`DOOR`, `bag_2_meat_RF`) | 1 | 6 (12%) |
| trigger | 34 | 19 / 8 / 7 | **type_CODE_name** for 33 of 34: `char_SN_Jack`, `loc_SN_nest_win` (convention at `trigger(BD):29`) | 1 | 0% |
| zephyr | 51 | 24 / 18 / 9 | Free lowercase: `harumech`, `naomicabin` | 1 | 0% |
| zephyr-special | 33 | 21 / 7 / 5 | Free: `Sheet_zero`, `Zero_home` | 1 | 0% |
| passport-rush | 33 (census 2026-09-27) | 12 / 8 / 13 | Free lowercase: `girl_v2`, `cop2_v2_noglasses`, `taxi_noroof`, `airport-inside` (one `prop_taxi`) | 1 | 9 (27%) |
| **Total (official)** | **2,142** | **890 / 785 / 467** | | **1 image each, 100%** | **31 of 2,142 (1%)** |

Findings:
- **Category values** are only `character`, `environment` and `prop`, each also appearing with an `auto:` prefix. `auto:` makes up 1,160 of the first 11 projects' 2,109 (55%; not recounted with Passport Rush). What `auto:` means is **not verified**; my guess is a platform-assigned category.
- **Every element has exactly one image**, in 11 of 11 official projects that use elements (kok-boru uses none), and `video_medias` is empty everywhere. The "multi-image element" does not exist in this data.
- **The description field is effectively unused** (1%). Descriptors live in the prompt text instead, as the "asset = text + image, descriptor pasted word for word" rule says (7 briefs: `adiliada:34`, `cully:44`, `hell-grind:21`, `if-you-stop:56`, `oneiric:26`, `red-flag:14`, `trigger(BD):25`).
- **Naming with a type prefix plus a project code** is used by 5 of 12: cully, oneiric and trigger in underscore style, if-you-stop in kebab style, and red-flag partially. That is **not a majority**. It is the documented convention in 4 briefs (`cully:57-58`, `oneiric:33`, `trigger(BD):29`, `if-you-stop:57`).
- **A state is its own element with its own name** (`_wet`, `_v4`, `army`, `-dead`). The data shows it (e.g. `char_RF_LEE_WET_v4`, `char_SN_Jackarmy`, `ch-kran-muzhik-dead`, passport-rush `taxi_noroof`, `cop2_v2_noglasses`) and 6 briefs say it (the Passport Rush brief does not) (`cully:55-56`, `hell-grind:47-48`, `if-you-stop:59`, `oneiric:35`, `trigger(BD):30`, `red-flag:41`).
- **How prompts store element mentions:** an `@tag` written in the UI is saved as `<<<element-uuid>>>`. I checked token against element id on up to 3,000 jobs per project, and tokens matched element ids in 9 official projects: all of them in 8 projects, and all but 4 of 8,073 in if-you-stop (`analysis5.txt`, bottom section). zephyr's 553 tokens matched no element id; the reason is unknown. kok-boru has no such tokens. Passport Rush was not part of this check. Positional images are saved as `<<<image_N>>>` (the UI syntax is `@[Image N](image_N)`, per `kok-boru:17`).

---

## 3. Asset folders: models, aspect ratios, sheet format

A folder counts as an asset folder when its path has an ASSETS / Characters / Locations / Props / Costume / Extras / Animals component and no test or regen component (`analysis4.py`). Counts are distinct image jobs.

| Project | Asset image jobs | Top image models in asset folders | Dominant AR / size (images) |
|---|---|---|---|
| adiliada | 3,263 | nano_banana_2 39%, soul_cinematic 23%, gpt_image_2 11%, soul_cinema_studio 9%, soul_location 7%, cinematic_studio_soul_location 6% | 21:9, 3:2, 2528×1088 (locations) |
| cully | 31,038 | seedream_v4_5 48.5%, nano_banana_2 31.8%, gpt_image_2 5.3%, soul_cinematic 5.0% | 21:9 (14,847), 16:9. Costume and character sheets are 16:9 (5,055 of 6,355 costume) |
| hell-grind | 8,906 | nano_banana_2, soul_cinematic, imagegen_2_0, image_auto, gpt_image_2 | 16:9 (4,314), 21:9, 2528×1088 |
| if-you-stop (Development/Props only) | 132 | seedream_v5_pro, nano_banana_2 | 16:9, 4:3 |
| oneiric | 20,697 | soul_cinematic 48%, seedream_v5_pro 10%, nano_banana_2, text2image_soul_v2, gpt_image_2, seedream_v4_5 | Locations 2528×1088 (5,815 of 9,205). Characters 2048×1152 / 16:9 |
| red-flag | 214 | seedream_v5_pro 80%, soul_cinematic 15% | 16:9 |
| trigger | 2,270 | characters: soul_cinematic 764 of 940. Locations, props, costume: seedream_v5_pro | Characters 2048×2048 (640), locations 2528×1088 |
| passport-rush | 4,495 | text2image_soul_v2 84.0%, seedream_v5_pro 15.2% (`Art_direction/Assets`, test and slop-check folders excluded; the private census script (not distributed) `asset_folder_images`) | not recounted |
| zephyr, zephyr-special, kok-boru, kok-boru-film | 0 | No image jobs in asset folders. zephyr keeps its images in `Iterations` (soul_cinematic 61% of all its images) | n/a |

Image-model usage across **all** image jobs per project (`analysis.txt:397-463`):
- **soul_cinematic** (the face / "Soul Cinema" model) is the #1 or #2 image model in **7 of 12**: oneiric, red-flag, trigger, zephyr and zephyr-special at #1, adiliada and hell-grind at #2. passport-rush does not use it; its characters come from Soul 2.0 (`text2image_soul_v2`, 3,841 jobs).
- **Nano Banana 2** has more than 100 jobs in **8 of 12**.
- **Seedream** (v4.5, v5 pro or v5 lite) has more than 100 jobs in **8 of 12** (passport-rush: seedream_v5_pro 1,035).
- **GPT Image 2** has more than 100 jobs in 5 of 12. (Recounted 2026-09-27 with the private census script (not distributed).)

**Aspect ratios of Soul Cinema images:** 2048×2048 (trigger characters), 2048×1152 or 2528×1088 (oneiric). Sheet composites are **16:9 landscape** (cully 5504×3072 nano_banana_2 sheets; hell-grind 16:9).

**Are character sheets 3-panel?**
- **Brief-stated 3-panel** in 2 of 12:
  - cully: "three panels in one image: a full body from the front, a full body from the back, and a large close portrait … three-quarter view", with heads removed from the full-body figures, a neutral grey background, no rim light, and empty hands (`cully:49-52`).
  - hell-grind: "A close-up of the face, a full body from the front, and a full body from the back … the front full-body figure has no head" (`hell-grind:22-23`).
- **Two-pass build** in 3 of 12: the face comes from Soul Cinema in close-up, the looks from Soul 2.0, and the sheet is assembled in Seedream, Nano Banana or ChatGPT without re-running the portrait (`adiliada:36-37`, `oneiric:38-39`, `trigger(BD):33-35`). zephyr says the same in a looser form: Soul 2, then Nano Banana 2 Pro and Seedream (`zephyr:29`).
- **Turnaround instead** (front, both profiles, back, portrait, expression map) for the 2D film if-you-stop (`if-you-stop:66`).
- **A turnaround video instead of a sheet** in passport-rush: "Instead of a traditional character sheet, we used a 360-degree character turnaround video that also included facial expressions and voice" (`passport-rush:24`). The video is attached to shots as a video reference (§5).
- **In the data:** prompts ask for "a full three-panel character reference sheet … LEFT PANEL: full body front … MIDDLE PANEL: back … RIGHT PANEL: close-up portrait … three quarter angle" (cully nano_banana_2, 16:9), "all three views (front, back, close-up)" (hell-grind edits), and "all three panels" (oneiric). Sheet wording appears in 1,089 of 4,000 oneiric and 200 of 280 cully character-folder image prompts (`analysis4.txt`).
- **Viewed (V):**
  - cully Kel_v3: 3-panel, headless front and back plus a 3/4 portrait, grey, 16:9.
  - hell-grind JAX: 3-panel, head masked out.
  - oneiric Rudy_s2_v1: 3-panel, headless, with a cut-out face, 5120×2880.
  - red-flag Lee_wet: 3-panel, but **the head stays on** and he holds a prop.
  - adiliada ADIL: **2-panel**, a full body with head plus a frontal close-up.
- **Verdict:** the grey 16:9 sheet with front, back and a large portrait is the dominant form in 4 of the 5 viewed sheets. The headless variant is brief-documented only by cully and hell-grind. I could not verify the sheet format for the other 7 projects.

Location plates: 2528×1088 (21:9) is the most common location-image size in oneiric, trigger, adiliada and cully. Two briefs bake the lens into the plate: "generate the location image with the anamorphic effect already in it" (`oneiric:44-49`, `trigger(BD):41-45`). Plates are three-quarter, not frontal (`cully:74`), and "generated for your future camera angles" (`hell-grind:52`).

---

## 4. Stress-test / test folders

| Project | Test folders (path) | Distinct jobs | Models |
|---|---|---|---|
| cully-hill-boys | `PRE  PROD/Character Test` (611), `Test_v1…v5`, `Test_scene16`, `ASSETS/TEST`, `ASSETS/LOCATIONS/tests`, and per-scene `test` folders (49, 40-45, 56-57, 65, 66, 67, 78, 105, 121, 137) | Character Test 611; Test_v4 7,095; Test_v3 573 (plus 1,533 in SCENE_8) | Character Test: seedance_2_0 331, text2image_soul_v2 136, soul_cinematic 88. Test_v4/v5 are mostly seedream_v4_5 and nano_banana_2 |
| oneiric | A `test` folder under **each asset**: 22 folders (characters Bob 24, Sam 20, Rudie 108, Alfie 72, Helen 115, Gorilla 50, Alien 21; extras; animals such as Basilisk 225; locations Lab 509, Kitchen 123, Livingroom 116, …) | 20–509 per asset | **seedance_2_0 almost 100%**, i.e. video motion tests of each asset |
| if-you-stop | `Development/{Animation_test 212, Characters_test/*, Locations_test/*, Scenes_Test 226, Stills_test 88}` | about 1,600 | Animation_test: seedance_2_0 196. The rest are image models |
| red-flag | `test` | 59 | seedance_2_0 55, cinematic_studio_video_3_5 4 |
| passport-rush | `Art_direction/Assets/4_Vid_Slop_Check/{1,2,3,4}` (1,368 jobs, seedance_2_5 1,243), `1_Characters/Girl_test`, `1_Characters/Ninjas_test` | about 1,500 (the brief says 1,545, `passport-rush:75`) | seedance_2_5, i.e. video checks of the assets |
| The other 7 | none | | |

**5 of 12** have test folders. What the briefs say a stress test is: "a video test … Ten generations — different actions, different shot sizes, different locations — recognizable in ten out of ten", run with the location and co-stars (`cully:62-65`), and "Ten generations in different poses and different light … not alone" (`hell-grind:37-38`). Oneiric's per-asset video test folders are the clearest instance in the data. Their counts (20–509) are larger than the ten the briefs name, so they include iteration beyond the ten. Oneiric's "Script Stress Test" (`oneiric:18`) is a script check, not an asset test. Passport Rush states the same rule in its own words: "A finished-looking image is not enough to validate an asset. It needs to be tested in video, under the same conditions in which it will appear in the film" (`passport-rush:98-102`).

---

## 5. Generation settings (distinct video jobs; `analysis.txt:72-395`)

| Project | Video jobs | Main video model (share) | Duration: mode (median) | Aspect ratio | Resolution | Audio on | Job-set size (takes per batch) |
|---|---|---|---|---|---|---|---|
| adiliada | 7,572 | seedance_2_0 100% | 15s (12) | 21:9 98.5% | 4k 55%, 1080p 33% | 99.7% | sets: 1×1,899 / 2×847 / 3×77 / 4×937 → mode 1 |
| cully | 408,723 | seedance_2_0 99.0% | 15s (15) | 21:9 97.6% | 1080p 82.7%, 4k 17% | 95.4% | size 4 = 91.6% of 106,681 sets |
| hell-grind | 102,476 | seedance_2_0 99.4% | 15s (15) | 21:9 92.0% | 1080p 93.8% | 99.7% | 4 = 55.7% |
| if-you-stop | 2,771 | seedance_2_0 93.2% (+minimax_h3 3.7%, seedance_2_5 3.1%) | 15s (10) | **4:3** 98.9% | 4k 83.7% | 96.3% | 4 = 81.6% |
| kok-boru | 2,664 | seedance_2_0 99.7% | 15s (15) | 21:9 99.2% | 1080p ~100% | 99.4% | 4 = 94.5% |
| kok-boru-film | 11,676 | seedance_2_0 99.8% | 15s (15) | 21:9 99.8% | 4k 84.1% | 98.7% | 4 = 72.5% |
| oneiric | 19,614 | seedance_2_0 98.4% | 15s (15) | 21:9 95.3% | 4k 85.5% | 99.4% | 4 = 53.5% |
| red-flag | 1,564 | **seedance_2_5 76.3%**, seedance_2_0 22.9% | 4s (7) | **16:9** 87.7% | 720p 74.9% | 100% | 4 = 88.8% |
| trigger | 3,033 | **seedance_2_5 93.3%** | **30s** (22) | 21:9 97.2% | 1080p 60.6%, 720p 34.7% | 99.8% | 4 = 75.8% |
| zephyr | 10,078 | seedance_2_0 100% | 5s (7) | **16:9** 99.8% | **720p** 100% | 99.2% | 1 = 100% |
| passport-rush | 9,663 | **seedance_2_5 99.3%** | **4s** (7) | **16:9** 98.1% | 1080p 96.3% | 99.8% | 4 = 48.7% of 3,647 sets |
| zephyr-special | 3,647 | seedance_2_0 97.4% | 15s (10) | 21:9 97.1% | 4k 59.2% | 98.8% | 4 = 61.3% |

Defaults and how many projects use each:
- **Seedance 2.0 is the main video model in 9 of 12.** Seedance 2.5 is the main model in red-flag and trigger, the two that use it for long takes, and in passport-rush, the newest (published 2026-09-25), which uses it for short 4–7 s shots. All 6 community projects use 2.5 (community 0 of 6 on 2.0).
- **15 s is the modal duration in 8 of 12** (passport-rush: 4 s; community 0 of 6). In `if-you-stop:128` the reason given is that duration must be set in the UI, "otherwise the model improvises the extra seconds".
- **21:9 in 8 of 12**, 16:9 in 3 (red-flag, zephyr, passport-rush), 4:3 in 1 (community: 21:9 in 5 of 6).
- **Resolution splits:** 4k in 5 of 12, 1080p in 5, 720p in 2.
- **Audio on in 12 of 12** (≥95%). The "no music" rule lives in the prompt (§6), not in a settings switch. One exception is documented: cully music scenes turn generation audio **off** (`cully:124`). cully has 18,463 audio-off jobs. That fits the brief, but I haven't checked that they are the music-scene jobs.
- **4 takes per batch is the modal job-set size in 10 of 12.** The exceptions are adiliada (mode 1) and zephyr (all singles). In passport-rush sets of 4 are 48.7%, singles 36.2% (the private census script (not distributed) `set_size_share`).
- **The UI multi-shot switch is off everywhere:** `multi_shots` is false in 100% of video jobs in 12 of 12, apart from 9 cully jobs. Cuts inside a clip are written into the prompt instead (§6).
- **Other UI params:** `genre`=`auto` ≥99% everywhere. `mode`=`std` for 2.0. `prompt_language`=`en` except zephyr, where it is `zh` for 7,717 of 10,075.
- **Input media:** besides elements, jobs attach `medias` with role `image` (the largest), `video`, `audio`, `start_image` and `end_image`. Start and end frames are rare: 653 `start_image` and 62 `end_image` in cully, fewer elsewhere. So "no shot is grown out of a still frame" (`cully:34`) holds in the data. **Video references are the exception that grew:** passport-rush attaches a video to 70.2% of its video jobs, mostly Blender grey-box previs for motion and camera and a 360° turnaround of the heroine for likeness (`passport-rush:24`, `:54-57`; `derived/passport-rush-methods.md`); the first 11 projects do it in at most 6% of their jobs.

---

## 6. Prompt structure (all distinct video prompts; `analysis3.txt`, `analysis5.txt`)

| Project | Distinct video prompts | Language | Length (median) | CINEDANCE header prevalence | ≥8 of the 16 headers | Timecodes | Cuts written in prompt (SHOT n / SEGMENT / HARD CUT) | Element refs `<<<uuid>>>` | Positional `<<<image_N>>>` |
|---|---|---|---|---|---|---|---|---|---|
| adiliada | 631 | en 100% | 1,857 words | SCENE CONTEXT 96, ACTIVE REF 97, PHYSICS 98, LIGHTING 96, AUDIO 94, FORMAT MODE 93, OPTICS 87, POSITIVE CONSTRAINTS 57 | **85%** | 51% | 77% | 13% | 61% |
| cully | 27,437 | en 88%, zh 11% | 1,501 words | AUDIO 86, CAMERA 79, LIGHTING 79, SCENE CONTEXT 76, STYLE 74, PHYSICS 74, ACTIVE REF 73, OPTICS 68, QUALITY 60, CHARACTER ACTING 29 | **71%** | 77% | 71% | 99% | 14% |
| oneiric | 2,058 | en 99% | 960 words | AUDIO 67, LIGHTING 66, SCENE CONTEXT 60, ACTIVE REF 60, POSITIVE LOCKS 60 (extra header), PHYSICS 59 | 47% | 69% | 32% | 83% | 43% |
| zephyr-special | 353 | en 97% | 874 words | CAMERA 43, ACTION TIMING 41, OPTICS 40 | 19% | 59% | 59% | 24% | 74% |
| if-you-stop | 518 | en 100% | 1,070 words | STYLE 24, AUDIO 19 | 10% | 30% | 5% | 85% | 39% |
| trigger | 455 | en 79%, zh 20% | 1,160 words | Uses its **own** skeleton: SOUND 73, FORBIDDEN 71, REFERENCES 59, ACTING TASK 48 (extra headers). Canonical 16: ~1% | 1% | 83% | 56% | 64% | 55% |
| hell-grind | 6,553 | en 87%, zh 13% | 1,072 words | CAMERA 28, STYLE 16, AUDIO 14, SCENE CONTEXT 13, GEO SPATIAL LAYOUT 2 | **0%** | 37% | 45% | 52% | 38% |
| kok-boru | 202 | en 97% | 1,743 words | ~0. Instead: `Style:` prefix (98%), `CHARACTER TAGS`, `DIRECTOR'S NOTES` | 0% | 0% | 93% | 0% | **95%** |
| kok-boru-film | 906 | **zh 57%**, en 43% | 6,189 chars (zh) | ~0. Instead: bracketed Chinese blocks such as 【风格】, 【光线】, 【空间地理】 and a style block (79%) | 0% | 22% | 54% | 33% | 79% |
| red-flag | 200 | **zh 89%** | 2,237 chars (zh) | ~0. Chinese blocks (78% zh headers: 场景背景, 激活参考, 动作时间轴 …) | 2% | 82% | 24% | 94% | 47% |
| zephyr | 1,174 | **zh 96%** | **348 chars** (short) | 0 | 0% | 0% | 3% | 3% | 77% |
| passport-rush | 962 | en 67%, zh 33% | 1,611 words (en) | English and Chinese blocks: HARD CUT, PHYSICS, SCENE CONTEXT, AUDIO, CAMERA, FORMAT MODE, LIGHTING, OPTICS, LOCATION MAP; 场景背景 / 有效参考 (`derived/passport-rush-methods.md`) | 32% (42.5% counting 场景背景 + 有效参考) | 39% | 46% | 18% | **85%** |

Language and length:
- **English in 9 of 12.** Chinese is the majority in zephyr, red-flag and kok-boru-film (passport-rush 33% Chinese).
- Prompts are **long in 11 of 12** (passport-rush median 1,611 English words): a median of 870–1,860 English words, or 2,200–6,200 Chinese characters. Only zephyr is short (median 348 characters).
- Hell Grind's brief says prompts "ran 3,000–4,000 words" (`hell-grind:72`). The mirrored median is 1,072, and the 90th percentile is 3,707.

Structure:
- **The CINEDANCE 15/16-block skeleton is the majority practice in only 2 of 12: adiliada (85% of prompts carry ≥8 of the headers) and cully (71%).** Oneiric is close at 47%, and its brief prescribes the same skeleton (`oneiric:61-63`).
- In **Hell Grind** the skeleton is **almost absent from the data** (0% ≥8 headers; `ACTIVE REFERENCES` 0%). Its brief presents the skeleton as "the formula … the version we would use from day one" (`hell-grind:11`), which is a retrospective recommendation, not what the mirrored prompts show.
- Sampled Hell Grind prompts use free forms instead, such as "EXACT 1 CHARACTER", role-labelled `<<<image_N>>>` lists, and FRAMING / SPATIAL / MOTION beats (`prompt_samples.txt`).
- **The brief-level skeletons differ between projects.**
  - cully's 15 blocks: `cully:85`.
  - hell-grind's example uses the same blocks, with a separate GEO SPATIAL LAYOUT: `hell-grind:70,78`.
  - adiliada and oneiric use a 12-block variant (… GAZE / EYELINES · SEGMENTS (timed beats) · DIALOGUE … POSITIVE LOCKS): `adiliada:47`, `oneiric:62`.
  - trigger uses 13 blocks (REFERENCES (ranked) · LOCKS · GEOGRAPHY / BLOCKING · FIRST FRAME · TIMELINE · DIALOGUE · ACTING TASK · … · SOUND · FORBIDDEN): `trigger(BD):71`. Its prompts follow it (SOUND 73%, FORBIDDEN 71%).
  - if-you-stop uses "style → context → location → first frame → optics → camera → timing → overlaps → lighting → audio → locks", or a short form of style line + @refs + one-line action + sound: `if-you-stop:104-109`.
  - kok-boru-film uses "Style Prefix" + "Constraints" paragraphs at the start: `kok-boru:15-19`.
  - The skill's own "final prompt architecture" is 12 sections, and it says "Do not treat every section as mandatory" (`skills/cinedance-v4-seedance.md:157-176`).
- **What is common across projects** is the order, not the exact headers. **5 of 12 briefs** (adiliada, cully, hell-grind, oneiric, trigger) open with SCENE CONTEXT, put references second, place a spatial map and first-frame block before camera and optics, write timed beats, and end with locks or constraints.

Majority prompt practices (from `analysis5.txt` phrase rates, with ≥50% of a project's prompts as the threshold):

| Practice | N of 12 | Projects |
|---|---|---|
| Written in English | 9 | all except zephyr, red-flag, kok-boru-film |
| "No music" written in the prompt | **10** | all except red-flag (48%) and if-you-stop (9%, where the brief says to name one diegetic sound explicitly: `if-you-stop:94`) |
| Lens stated in mm, FOV° or a Chinese lens word | 9 | not red-flag (48%), if-you-stop, zephyr; oneiric 54% (47% in mm alone); passport-rush 54% (`SEGMENT 1–2 LENS LOCK = 84°`) |
| Duration stated in the prompt | 8 | zephyr-special, trigger, red-flag, oneiric, hell-grind, cully, adiliada, passport-rush (73%) |
| Per-beat timecodes (e.g. `0.0s–3.0s`, `0-0.6秒`) | 6 — half, not a majority | trigger, red-flag, cully, oneiric, adiliada, zephyr-special (passport-rush 39%) |
| Several shots or cuts inside one generation (SHOT n / SEGMENT n / HARD CUT / CUT TO / 硬切 / 第一镜) | **7** | kok-boru, adiliada, cully, zephyr-special, trigger, kok-boru-film, hell-grind (50.4%, on the line; the 2026-09-24 count had it at 45% with a pattern that was not kept, which gave 6 among the first 11). passport-rush 46% |
| Elements referenced by tag (`<<<uuid>>>`) | 6 — half, not a majority | cully, red-flag, if-you-stop, oneiric, trigger, hell-grind (passport-rush 18%) |
| Positional `<<<image_N>>>` | **7** | kok-boru, kok-boru-film, zephyr, zephyr-special, adiliada, trigger, passport-rush (85%) |
| Prompt carries a style prefix or STYLE block | 5 | kok-boru 95%, kok-boru-film 78%, cully 78%, passport-rush 61%, zephyr-special 59%. hell-grind is at 48% |
| "100% matches the reference" lock | 3 | adiliada, cully, oneiric (passport-rush 44%) |
| CINEDANCE ≥8-header skeleton | **2** | adiliada, cully (only near-majority in oneiric; passport-rush 32%, 42.5% counting 场景背景 + 有效参考) |

- Every project used either `<<<uuid>>>` or `<<<image_N>>>` references in the majority of its prompts: **12 of 12**. The 2026-09-27 rows above were recounted with the private census script (not distributed); it reproduces the published 11-project numbers except the cuts row, where hell-grind sits on the 50% line.

**Chinese-aware recount (2026-09-25).** The header regexes above match English headers only, so Chinese-majority projects are undercounted. A per-job grep over the raw ledger (research-auditor, 2026-09-25) found:
- red-flag carries the same skeleton in Chinese (场景背景 / 激活参考 / 首帧 / 拍摄模式 / 光学镜头 / 摄影机 / 动作时间轴 / 表演任务 / 物理 / 灯光 / 对白与声音 / 正向锁定): 场景背景 and 激活参考 appear together in 1,028 of 1,564 video jobs (66%). The skeleton is therefore majority practice in **3 of 12** (adiliada, cully, red-flag), still a minority.
- A standalone acting-task block is majority in red-flag too (表演任务 in 956 of 1,564 video jobs, 61%), besides trigger's ACTING TASK.
- Slow motion is mostly written as a **ban**, not a request: red-flag "绝无慢动作" / "绝对禁止：慢动作", and trigger's FORBIDDEN lists ("slow motion, speed ramping, handheld…", 1,276 of 1,859 trigger lines that mention it). Genuine slow-motion requests are rare. A platform filter on the words refuses these bans too.
- A formal `environment` element is referenced by the majority of video jobs in **4 of 12** (cully 91.0%, red-flag 72.6%, oneiric 55.4%, if-you-stop 53.8%; passport-rush 8%); the others place location in prose or positional images. No element of any style category exists in any official job (only character / environment / prop, with or without `auto:`).
- Treat every header percentage in this section as a lower bound for red-flag, trigger, kok-boru-film and zephyr.
- **Multi-shot vs single take:** 7 of 12 write several shots into one generation (hell-grind on the 50% line), while the UI multi-shot switch stays off. The single-take projects are if-you-stop (5%), zephyr (3%) and red-flag (24%). red-flag's fight money-shot is deliberately "one continuous take … timeline in seconds" (`red-flag:49`).

---

## 7. Brief structure (section headings with line numbers)

| Section | Projects (brief:line) | N of 12 |
|---|---|---|
| Logline | cully:2, red-flag:1, if-you-stop:4, oneiric:3, kok-boru-film (`kok-boru:2`), kok-boru(BD):2, passport-rush:3 | 7 |
| About the project | adiliada:8, cully:4, hell-grind:3, if-you-stop:8, oneiric:7, red-flag:5, trigger(BD):5, kok-boru-film `kok-boru:6`, kok-boru(BD):5, passport-rush:7 | **10** |
| Tools (heading or paragraph) | adiliada:25, cully:10, hell-grind:9, if-you-stop:19, oneiric:12, red-flag:8, trigger(BD):9, passport-rush:54-56 (Blender) | **8** |
| The numbers / production data | cully:7, hell-grind:9, if-you-stop:182, passport-rush:71-89 | 4 |
| Development | adiliada:29, oneiric:16, trigger(BD):14, if-you-stop:36 (+cully "Breakdown and the shotlist" :35) | 4–5 |
| Pre-production (assets, prompts, voice) | adiliada:32, cully:42/83, hell-grind:20/60, if-you-stop:54, oneiric:24/53, red-flag:12/19, trigger(BD):23, kok-boru-film `kok-boru:10`, kok-boru(BD):9/13, passport-rush:11 | **10** |
| Production | adiliada:44, cully:131, oneiric:59/70/87, red-flag:29/38/47, trigger(BD):68, kok-boru-film `kok-boru:22`, kok-boru(BD):21, passport-rush:61 | **8** |
| Post-production | adiliada:49, cully:143, hell-grind:114, if-you-stop:170, oneiric:100, trigger(BD):98, kok-boru-film `kok-boru:24`, kok-boru(BD):25, passport-rush:120 | **9** |
| Conclusion | adiliada:54, cully:149, hell-grind:119, if-you-stop:199, oneiric:111, trigger(BD):107 (passport-rush ends with "Learnings" :91 and Post-Production :120, no conclusion) | 6 |
| What's attached | cully:154, hell-grind:124 | 2 |
| Failed approaches | if-you-stop:191 | 1 |

zephyr and zephyr-special are narrative production notes with no stage headings (`zephyr:1-63`, `zephyr-special:1-38`).

**Canonical brief outline (majority):** Logline → About the project → Tools → Pre-production (assets → prompts) → Production → Post-production → Conclusion, with a numbered form `01 · DEVELOPMENT … 0N · CONCLUSION` in 4 of 12 (adiliada, oneiric, trigger, if-you-stop). Conclusion is 6 of 12, half: every other section in this outline is a majority.

Recurring content inside briefs:
- **Asset = text + image, descriptor pasted word for word:** 7 of 12 (the Passport Rush brief does not say it).
- **A state is its own asset:** 6 of 12, half, so no longer a majority (the Passport Rush brief does not say it; its elements do, §2).
- **A style prefix or fixed technique line pasted into every prompt:** 7 (passport-rush:114-118 "Lock the lighting language … Suggested style prefix", although that prefix appears verbatim in 0 of its prompts, `derived/passport-rush-methods.md`; hell-grind:73-74, red-flag:21-22, kok-boru-film `kok-boru:16-19`, kok-boru(BD):15-18, cully:133, if-you-stop:34).
- **"No music / SFX only":** 6 (hell-grind:76, cully:147, trigger(BD):105, kok-boru:21, kok-boru(BD):20, if-you-stop:94).
- **Behaviour or acting task, not emotion words:** 5 (cully:109-113, hell-grind:89-95, oneiric:88-97, trigger(BD):89-95, if-you-stop:149-151).
- **Voice as a fixed written condition pasted verbatim:** 4 (cully:66-68, hell-grind:39-42, oneiric:54-56, trigger(BD):64-66).
- **Canvas as the asset board:** 6 (passport-rush:39, cully:60, hell-grind:16, if-you-stop:88, kok-boru:11, kok-boru(BD):10).
- **Diagram or staging map:** 3 (oneiric:71-84, trigger(BD):77-83, red-flag:31).
- **First-second master wide:** 2 (cully:98, hell-grind:83-86).

---

## 8. Review and selection on the platform

| Field (per job) | Result, all 12 official |
|---|---|
| `is_favourite` true | **0** of 707,532 official jobs (692,924 in the first 11 + 14,608 in passport-rush) |
| `comments_count` > 0 / `has_unresolved_comment` | 0 / 0 |
| `review_status` not null | 0 |
| `board_ids` non-empty | 0 |
| `published_at` set | 0 |

Source: `analysis.txt:501-518`; passport-rush checked 2026-09-27 with the private census script (not distributed) (`review_fields_nonzero` empty over 14,608 jobs). The community projects are also all zero.

The mirror is a **published snapshot**. 1,696 of the first 11 projects' 1,698 non-root folders have `is_snapshot: true`, and `publication.json` carries both `snapshot_folder_id` and `original_folder_id`. So these fields may have been **stripped at publish time or never used; the data cannot tell which.** No platform-level review evidence survives.

Selection evidence that does survive is indirect:
1. **Folder copies.** zephyr's `Production` is 266 of 275 jobs re-filed from `Iterations`. adiliada re-files 255 asset jobs across parts. cully files 183 jobs in both `37 Scene` and `37 Scene/Regenerate`.
2. **Regeneration folders** in 6 of 12 (§1b). Passport Rush says what they are for: "we first assembled a full cut of the film, then regenerated individual scenes" (`passport-rush:69`).
3. **Brief-described review happens off-platform:** a version log of "version, what changed, verdict" (`cully:134`, `hell-grind:106`), a "generation supervision" pass after the rough cut (`oneiric:105`, `trigger(BD):100`, `cully:145`), and the editor ordering missing shots during generation (`cully:141`, `hell-grind:115`).
4. No official project has a "final", "selects" or "approved" folder. Only the community project aist__detour does (`FINAL`, `approved`, `trash`).

---

## 9. Pipeline stages named in the briefs

| Brief | Stages as named (brief:line) |
|---|---|
| hell-grind | "**the 11-stage production pipeline**" is named **only** in the attachment list (`hell-grind:125`). **The attachment is not available.** `attachments/CHECKLIST.md` (hell-grind:125, item 6) marks it UNKNOWN: "三口皆无" (absent from all three public endpoints). **The 11 stages cannot be listed.** The brief body gives: asset canvas (16) → sheets (22-35) → stress test (37-38) → voice lock (39-42) → behaviour profile (43-44) → states (47-48) → location angles (52) → prompts with Claude skills (60-86) → batches and log, 10–15 iteration rule (103-106) → edit in parallel, cleanup, color, sound (114-118) |
| oneiric | Chapters: 01 Development (16) · 02–03 Pre-production (24, 53) · 04–06 Production (59, 70, 87) · 07 Post-production (100) · 08 Conclusion (111). **Edit, "five stages to picture lock":** 01 Assembly · 02 Rough cut · 03 Generation supervision · 04 Fine cut · 05 Picture lock (`oneiric:102-107`) |
| trigger | 01 Development (14) · 02 Pre-production (23) · 03 Production (68) · 04 Post-production (98) with **the same five edit stages** (`trigger(BD):99-100`) · 05 Conclusion (107) |
| adiliada | 01 Development (write and argue each scene, then a step-by-step storyboard; 29-31) · 02 Pre-production (assets, sheets, locations; 32-43) · 03 Production (CINEDANCE; 44-48) · 04 Post-production (cleanup, color, sound; 49-51) · 05 Conclusion (54) |
| cully | Breakdown and shot cards (35-40) → Pre-Production: Assets (42) → stress test (62) → voice (66) → locations (71) → look and color bible (78) → Pre-Production: Prompts (83) → Production: organization and log (131-134) → edit in parallel (140) → Post-Production: polish, generation supervising, color, voice cleanup and sound (143-147) |
| if-you-stop | 01 Development (36) · 02 Assets & Pre-production (54) · 03 Sound & Effects (92) · 04 Prompting (101) · 05 Staging (141) · 06 Performance (148) · 07 Post-production (170) · 08 Production Data (182) · 09 Failed Approaches (191) · 10 Conclusions (199). No storyboard or animatic; first takes serve as the animatic (`:46-47`) |
| kok-boru-film | Pre-Production: Assets on Canvas (`kok-boru:10`) → Preparing prompts for Video (14) → Production: Folder Structure (22) → Post-Production: Color, Music, Sound, Voice (24) |
| kok-boru (trailer) | The same four, and the last folder is "our earliest experiments" (`kok-boru(BD):9-25`) |
| red-flag | 01–02 Pre-production (12, 19) · 03–05 Production (29, 38, 47). The mirrored brief ends at 05 |
| passport-rush | Pre-Production: idea, script, storyboards, assets (11-12) · Assets (13) · Storyboard (42) · Blender previs (54-57) · Production: art direction, then video generation in five sequences (61-69), a full cut assembled, then scenes regenerated (69) · Learnings (91) · Post-Production: colour grading, original score, sound design, all by hand (120-123) |
| zephyr / zephyr-special | No stages named. "hadn't yet established an efficient fixed pipeline or a unified style prefix" (`zephyr:60`) |

**Common pipeline (majority):** Development / breakdown → Pre-production (assets, then prompts) → Production (batch generation with versioned iteration) → Post-production (cleanup, then color unification, then sound). Post-production and pre-production are named in 9 and 10 of 12 briefs. The five-stage edit is **2 of 12** (oneiric, trigger).

---

## 10. Community projects (secondary; not counted above)

| Project | Folder skeleton | Video settings | Elements | Prompt structure | Brief sections (BD) |
|---|---|---|---|---|---|
| aist__detour | Animation/{Audio, Scene 01–12}, Characters/*/FINAL, Locations, Props, Tests, trash, cover | seedance_2_5 98%, 21:9, 720p, mode 30s | 132, free or kebab names | en/zh/ru mix, median 402 words, ≈no CINEDANCE | Context / Narrative / Visuals / Production / Status |
| allan_ripley__the-prompter | Characters, Locations, Props, Key Art, Promo, SCENE 1–7, Skills, Upscaled SD2.5 | seedance_2_5 99.5%, 21:9, 720p, mode 20s | 124, **100% `char_TP_…_v1`** | **Full 15-block skeleton in 89%**, median 2,045 words | Note of intent / The film / Tools / Worlds / Characters / Sound / Discipline / Skills / Geography / Acting / Continuity / Status. Cites the "fifteen-block skeleton" and CINEDANCE (`the-prompter(BD):33-35`) |
| fotachu__one-day-with-mr-otter | Scene-by-location folders (HOLLYWOOD SCENES, NEW YORK …), POSTERS, TEXT | seedance_2_5 98%, 21:9, 1080p, mode 30s | none | Short prose, median 260 words | Context / Narrative / Visuals / Production / Resources |
| graffitiavocado1127__la-noche-triste | Characters and Props (empty), Location reference, action-named folders | seedance_2_5 98.7%, 16:9, 1080p, mode 30s | 106, `char_*` / `prop_*` | CINEDANCE-like (≥8 headers) in 86%, median 1,653 words | Narrative essay (why AI, two language versions) |
| jacob_everett__fallen-leaves | Only `Watermarks`; items sit in the root | seedance_2_5 99%, 21:9 / 16:9, mode 8s | 10, plain names | Prose, median 421 words | Premise / Setting / Tone & Visual Style / Themes / Production |
| zhit__the-patch | Characters, Locations, Props, `SC_001…005/001_01…`, Sound, Titles | seedance_2_5 99.5%, 21:9, 480p, mode 4s | 28 | en, median 1,105 words, CAMERA header only | Context / Narrative / Visuals / Main characters / Status |

All 6 community projects use **Seedance 2.5** as their main model, while the official projects mostly use 2.0. That is consistent with later production dates, but **I haven't verified the dates.**

---

## 11. Canonical spec (derived from the counts above; each line tagged with its N of 12)

1. **Folder skeleton:**
   - Root holds `ASSETS/` plus scene folders (9 of 12 each).
   - Assets are split by type into characters, locations, props, and optionally costume, extras and animals (6 of 12).
   - Scenes are named `SCENE NN - NAME` (the word "scene" appears in 5 of 12). Shot folders go inside scenes (6 of 12).
   - Optional folders: `regenerations/` (6 of 12), `test/` (5 of 12), and title or credits as its own scene (6 of 12).
2. **Elements:**
   - One image per element (11 of 11 official projects that use elements).
   - Category is character, environment or prop.
   - The description field stays empty; the descriptor lives in the prompt.
   - The name is `type_CODE_name[_sN][_vN]` (5 of 12 plus 4 briefs). This is the documented convention, not a majority practice.
   - Each state is a new element (6 of 12 briefs, half; the elements of passport-rush do it too, §2).
3. **Assets:**
   - The face comes from Soul Cinema (soul_cinematic is the #1 or #2 image model in 7 of 12; passport-rush uses Soul 2.0).
   - The sheet is assembled or edited in Seedream, Nano Banana 2 or GPT Image 2 (each has >100 jobs in 8, 8 and 5 of 12). passport-rush uses a 360° turnaround video instead of a sheet (`passport-rush:24`).
   - The sheet is a 16:9 grey sheet with front and back full-body views plus a large 3/4 portrait (4 of 5 viewed). Headless bodies are documented by cully and hell-grind only.
   - Location plates are 21:9, 2528×1088.
4. **Stress test:** video tests of each asset (5 of 12 have test folders; passport-rush with Seedance 2.5, `passport-rush:98-102`). "Ten in ten" is stated in 2 briefs.
5. **Generation defaults:** Seedance 2.0 (9 of 12; the newest, passport-rush, and all community projects use 2.5), 15 s (8), 21:9 (8), audio on (12), 4 takes per batch (10), UI multi-shot off (12). Resolution is split between 4k and 1080p.
6. **Video prompt:**
   - English (9), long (11).
   - "No music" written in (10), lens stated (9), duration stated (8 of 12; recounted 2026-09-27, method below).
   - Timed beats (6 of 12, half), multi-shot written in text (7 of 12; hell-grind on the line), references by tag or position (12). References **by tag alone** are 6 of 12: cully, hell-grind, if-you-stop, oneiric, red-flag and trigger use element tokens `<<<uuid>>>` in more than half of their prompts (§6 table); adiliada, kok-boru, kok-boru-film, zephyr, zephyr-special and passport-rush refer mostly by image position (positional references are the majority form in 7 of 12).
   - Descriptors are pasted **and adapted per shot**, not only pasted: oneiric's prompt `e668a8a7` gives `char_ON_Sam_s2_v1` as "slim 18-year-old man … a floppy slice of pizza in his hand; deep inside the story he is telling", the shot's own state; trigger `06c35f81` writes "JACK <tag> — lean man ~45 in a black knitted wool balaclava …". How often descriptors are adapted was not counted.
   - **Identical re-fires are common:** the same prompt, duration, resolution and media re-fired as a new batch, per job set in time order — trigger 366 of 864 batches (42%), red-flag 197 of 409 (48%), oneiric 3,075 of 5,957 (52%) (2026-09-26 audit, verified by the main session), passport-rush 2,475 of 3,647 (68%; recounted 2026-09-27 with the census script, which reproduces trigger and red-flag exactly and oneiric within 4 batches).

   **Duration recount (2026-09-27, the private census script (not distributed)).** Distinct video prompts per project; a prompt states a duration when `production/prompt.py`'s pattern matches (N s, N–M seconds, N秒; ages such as "late-20s" excluded). Share per project: trigger 0.98, cully 0.94, adiliada 0.88, oneiric 0.87, red-flag 0.85, hell-grind 0.78, zephyr-special 0.69 — then kok-boru-film 0.46, kok-boru 0.40, if-you-stop 0.37, zephyr 0.18. At a 50% threshold that was 7 of the first 11 (9 at 40%, 7 at 60%); passport-rush 0.73 makes it **8 of 12**. Counting only statements of the total duration (not timed beats) also gives 7 at 50% (6 at 60%, 8 at 40%). The private census script (not distributed), which dedupes by job id, gives slightly different shares (adiliada 0.87, red-flag 0.80) and the same counts. The 2026-09-26 audit's "4–6" did not reproduce, and its method was not recorded.
   - The block order SCENE CONTEXT → REFERENCES → MAP / FIRST FRAME → FORMAT / OPTICS / CAMERA → TIMING → PHYSICS / LIGHTING / AUDIO / ACTING → STYLE / QUALITY → POSITIVE LOCKS is **documented in 5 briefs but practised as a majority only by adiliada and cully**.
7. **Brief:** Logline (7) · About (10) · Tools (8) · Pre-production (10) · Production (8) · Post-production (9) · Conclusion (6) — of 12.
8. **Pipeline:** Development → Pre-production → Production → Post-production (9–10 of 12). The edit has five stages to picture lock in 2 of 12. The Hell Grind "11 stages" are **unavailable**.

## 12. What I could not verify

- **Hell Grind's 11-stage pipeline, team guide, handbook and shotlists:** not in any mirrored source (`attachments/CHECKLIST.md`, hell-grind:125).
- **Whether the review fields** (favourite, comments, review_status, boards) were stripped by the snapshot or never used.
- **The meaning of `auto:` element categories.**
- **Why zephyr's 553 `<<<uuid>>>` tokens match no element id.**
- **Character-sheet layout** for the projects whose sheets I did not view (trigger, zephyr, zephyr-special, kok-boru, kok-boru-film, if-you-stop). For these I relied on brief text, prompt wording and aspect ratios only.
- **The red-flag brief** in both the clean and decoded copies ends at "05 · PRODUCTION". Whether a post-production section existed upstream is unknown.
- **trigger's brief has empty spots:** the grade table and "iron rules" list at `trigger(BD):53-62,73-75` decode as empty lines, probably embedded tables or images, so their content is unknown.
- **Header percentages are lower bounds** because detection needs the header at the start of a line in upper case.
- **Chinese "words" are counted as characters**, so length is not comparable across languages.
- **Job-set size counts only mirrored jobs.** Takes deleted before publishing are invisible.
- **The 2026-09-24 regexes were lost.** The 2026-09-27 census script rebuilds them; for per-beat timecodes, cuts, style prefix and lens it had to be calibrated until the published 11-project counts came back, so a project near 50% (hell-grind on cuts) can move by one.

## 13. HF process checklist: what the platform defaults to and what stays guidance (2026-09-25)

Built from the 3 manuals (cd = cinedance-v4-seedance.md, ac = acting-system.md, li = lira-image-prompts.md), the briefs and this census; every label was re-checked by a research auditor against the cited lines. Rule for MVGP (owner 2026-09-25, "必须学HF", "没必要自己发挥"): **MAJORITY practice is the platform default; MINORITY practice and manual-only rules are guidance in AGENT_GUIDE; no craft rule blocks a take.** `(BD)` = decoded brief, counted from the census.

**Majority practice (platform default; item 2 is half of the 12 and stays the platform's form)**
1. Asset = a text descriptor pasted word for word into every prompt + one anchor image — MAJORITY (7 of 12): adiliada:34, cully:44, hell-grind:21, if-you-stop:56, oneiric:26, red-flag:14, trigger(BD):25.
2. Each state is its own named element — HALF (6 of 12 briefs: cully:55, hell-grind:48, if-you-stop:59, oneiric:35, red-flag:41, trigger(BD):30; passport-rush's elements do it too, e.g. `taxi_noroof`, §2). It was a majority (6) among the first 11; kept as the platform's form because an element holds one image (§2).
3. One image per element; category character / environment / prop; description field empty — 11 of 11 that use elements (§2).
4. Project root = assets area + scene folders; shots inside scenes — MAJORITY (9 of 12; §1b); kok-boru:23.
5. Generation defaults: audio on (12), 4 takes per batch (10), UI multi-shot off (12), references by tag or position (12; by tag alone 6 of 12, by position 7 of 12) (§5, §6); an identical prompt re-fired as a new batch in 42% / 48% / 52% / 68% of batches (trigger / red-flag / oneiric / passport-rush; §11).
6. Prompt: English (9), long (11), "no music" (10), lens stated in mm or FOV° (9), duration stated in the prompt (8 of 12, recounted 2026-09-27; if-you-stop:128 gives the reason) (§6, §11); descriptors pasted and adapted per shot (§11); several shots inside one generation (7 of 12, hell-grind on the 50% line; owner Q2).
7. Stages Development → Pre-production → Production → Post-production (9–10 of 12 briefs; §9).

**Minority practice and manual rules (guidance only, never checked)**
- Development: scene argued as want / obstacle / turn (4–5 of 12: adiliada:30, oneiric:18); storyboard (3 of 12, incl. passport-rush:42-52 with Blender shots marked; if-you-stop:46 had none); Cully's four-group shot card (cully:36-39).
- Assets: 16:9 grey 3-panel sheet, front + back full body + large 3/4 portrait (4 of 5 viewed sheets; headless in cully:49-50, hell-grind:22-23 only); two-pass face then body (3 briefs); element naming `type_CODE_name[_sN][_vN]` (5 of 12); location plates three-quarter with the lens baked in (cully:74, oneiric:44-49); stress test ten in ten (5 of 12 have test folders; cully:63, hell-grind:38, passport-rush:98-102 "test key assets in motion early"; a switch, default off); canvas as the asset board (6 of 12); a 360° turnaround video with expressions and voice instead of a sheet (1 of 12: passport-rush:24).
- Voice and acting: voice as fixed written text pasted verbatim, "Voice is not an asset" (4 of 12: cully:67, hell-grind:39-40, oneiric:55; ac:265); behaviour and tactics instead of emotion words (5 of 12: cully:109-113, hell-grind:89-95, oneiric:88-97, if-you-stop:149-151); master acting profile 150–220 words, one flowing paragraph, adapted per scene, led by the @tag (ac:158, ac:236-259); eye life (ac:215-234).
- Prompts: CINEDANCE skeleton (3 of 12 incl. red-flag in Chinese; cd:157-176 "do not treat every section as mandatory"); first frame already occupied (5 briefs; cd:336, cully:31); lens as diagonal FOV degrees (cd:567, cully:92 — cully's own prompts also give mm); no standalone negative block (cd:1176); silent self-QA of 20 questions (cd:1272-1297); style prefix pasted verbatim (7 of 12 briefs, majority of prompts in 5 of 12); per-beat timecodes (6 of 12, half; a majority among the first 11); a video as the motion, camera or likeness reference (1 of 12 as a majority practice: passport-rush 70% of video jobs, Blender previs and a turnaround video, `passport-rush:24`, `:54-57`).
- Production: one-line (one-block) patch of a failed prompt, word for word otherwise — MINORITY (2 of 12: cully:134, hell-grind:106; cully:25 "patches only the section that failed"); version log "version / what changed / verdict" (cully:134, 137 entries; hell-grind:106); 10–15 iterations then simplify the shot, not the wording (hell-grind:106; if-you-stop:206); regenerations folder (6 of 12); edit in parallel with generation (cully:140-141, hell-grind:114-115); generation supervision after the rough cut (4 of 12, incl. passport-rush:69 "first assembled a full cut of the film, then regenerated individual scenes").
- Post-production (deferred on the platform): colour unification (5 of 12, incl. passport-rush:121); five edit stages to picture lock (oneiric:102-107, trigger(BD):99-100); trim the first and last half-second of every clip (cully:141, hell-grind:115); cleanup before colour (2–3 of 12).

**Conflict between a manual and the census**: cd:207-209 says to omit settings the UI controls, including duration; 8 of 12 projects state duration in the prompt anyway (if-you-stop:128: "otherwise the model improvises the extra seconds"). Practice wins: the prompt states the duration.

**Unavailable**: Hell Grind's 11-stage pipeline, team guide and shotlists (attachments/CHECKLIST.md hell-grind:125); Cully's standalone bibles (text lives inline in the brief only).
