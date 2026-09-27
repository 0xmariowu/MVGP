# Production tests: what each file guards

The platform keeps tests only where a failure would cost money, lose the owner's picks or film,
wrongly block a take HF would accept, or let someone other than the owner act. Run the whole kept suite with:

```sh
.venv-production/bin/python -m unittest -q $(ls production/tests/test_*.py | sed 's#/#.#g; s#\.py$##')
```

Shared fixtures: `fixtures.py` (owner login, fake provider, test runtime config), `writer_fixture.py` (a writer's
authored card), `test_craft_journey.py` (the full-film world, no tests of its own), `data_hf_era_documents.json`
(the route and cost documents the test world runs on), `data_release97_execution_policy.json` (the live cost
policy that `runtime.json` must equal).

## Money: nothing is paid twice, re-priced or lost

| File | Guards |
|---|---|
| test_submissions | cost frozen at submit, refused if runtime.json's price changes, one reservation per take |
| test_batches | a batch reserves its whole cost before any take; four repeats of one card; no shared-envelope overspend |
| test_jobs | fenced dispatch, one paid call per job, bounded result download, unknown outcomes keep their hold |
| test_worker | restarts poll instead of resending; timeouts become unknown, never retried; completions and expiry |
| test_recovery_e2e | a crash at any point never repeats the paid effect; backup/restore keeps the holds |
| test_boundary_e2e | HTTP replays make one paid dispatch; a price change refuses a queued job |
| test_provider_fal, test_provider_apilio | the two adapters: fixed hosts, no key in fake mode, a lost answer is never resent |
| test_billing, test_operations | operator bill reconciliation, abandon and envelope commands keep unknowns visible |
| test_reader, test_frame_evidence | the Gemini reader's budgeted calls and its frames |
| test_runtime_config | runtime.json is release 97's cost policy; every price loads; routes the compiler accepts |

## The owner's picks and the film

| File | Guards |
|---|---|
| test_decisions | only the owner's session picks and confirms; a pick change supersedes the pending film |
| test_assembly, test_cuts | picks become one cut in shot order; the render keeps exact trims, sound and sources |
| test_shoot | one call shoots a scene and the takes reach the desk |
| test_queries, test_web_app, test_browser, test_web_shell | the desk shows every take, pick and film; old records still render |
| test_owner_notes, test_switches | the owner's notes and switches are written by his session only |
| test_patches | a scoped repair keeps authored intent and take history |

## A take HF would accept is never wrongly blocked

| File | Guards |
|---|---|
| test_prompt, test_compiler | the sent prompt is the writer's text plus only the allowed additions |
| test_gates | only send integrity, money and provider limits block; craft findings are advice |
| test_asset_methods | image prompts and references go out exactly as prepared |
| test_output_contract | delivered pixels are measured against the route's output contract (test_jobs: the frozen one) |
| test_context, test_workflow | the writer sees the whole scene; stale inputs are reported, not hidden |

## Only the owner and his agents act

| File | Guards |
|---|---|
| test_owner_login, test_access_identity, test_access_keys | the desk session needs the owner's signed Access identity |
| test_auth, test_api, test_cli, test_server, test_contracts, test_projects, test_media | scoped credentials, private kinds, upload and path boundaries |
| test_store, test_runtime_storage, test_record_payloads, test_review_payloads, test_review_http | durable store, private storage paths, externalized records, bounded private HTTP |
| test_reviews, test_journeys, test_playbook | agent feedback never becomes the owner's acceptance; agent journeys over CLI/HTTP; the manuals ship with the code |
