"""Owner switches per film: which optional steps run.

Higgsfield has none of these steps: the writer re-checks each prompt while writing it and people pick
takes by eye. Owner 2026-09-24: shot-plan approval, the AI prompt review and the AI director are
switches, all off by default. The defaults are declared by the release's execution policy
(`owner_switches`); the live release declares them off. A release without that key keeps the earlier
behaviour (every step on). Deterministic gates, budgets and the owner's picks are not switches.
"""
from __future__ import annotations

import json
import sqlite3
from typing import Annotated, Any, Literal

from pydantic import StringConstraints

from production.auth import AuthService, Principal
from production.contracts import DomainError, Mutation
from production.store import Store

DEFAULTS: dict[str, bool] = {'shot_plan_approval': False, 'ai_prompt_review': False, 'ai_director_review': False,
                             'asset_stress_test': False, 'source_reading': False, 'sample_mode': False}
# the owner's stress-test switch is off unless he turns it on, whatever a
# release declares. The three older keys are dormant (production/DORMANT.md); nothing reads them on a path.
# (owner 2026-09-27): 样片模式 sends a film's shots to fal's 480p drafts; off, they go to Higgsfield.
OFF_UNLESS_SET = frozenset({'asset_stress_test', 'source_reading', 'sample_mode'})
# (owner: "复刻要先让 Gemini 看原片…应该是有开关"): on by default for a recreation project
# created with this switch (projects.py marks new projects `reader_switch`); the two earlier test films keep
# their behaviour. When on, a recreation shot needs a source-understanding citing the reader's observation.
def _reading_default(store: Store, pid: str, conn: sqlite3.Connection | None) -> bool:
    try:
        body = store.get_object(pid, pid, conn=conn)['body']
    except DomainError:
        return False
    return body.get('branch') == 'recreation' and body.get('reader_switch') is True
OBJECT_ID = 'owner_switches'  # the first record's id; object ids are unique across the whole store


def object_id(pid: str) -> str:
    """One switches record per film (simulated run: a fixed id let only one project ever set a switch)."""
    return f"{OBJECT_ID}_{pid.removeprefix('project_')}"


def _stored(store: Store, pid: str, conn: sqlite3.Connection | None) -> dict[str, Any] | None:
    for oid in (object_id(pid), OBJECT_ID):  # the per-film record, else a record written under the first id
        try:
            obj = store.get_object(pid, oid, conn=conn)
        except DomainError as exc:
            if exc.code != 'not_found':
                raise
            continue
        if obj['kind'] == KIND and obj['author'] == AUTHOR:
            return obj
    return None
KIND = 'owner-switches'
AUTHOR = 'owner_settings_service'
# The desk labels of the switches. Asset qualification is not a switch.
LABELS = {'shot_plan_approval': 'shot-plan approval', 'ai_prompt_review': 'the AI prompt review',
          'ai_director_review': 'the AI director review', 'asset_stress_test': 'the asset stress test',
          'source_reading': 'reading the source first (recreation)', 'sample_mode': '样片模式 (fal 480p drafts)'}


class SwitchesUpdate(Mutation):
    # only the stress-test key can be set; the three dormant keys toggle nothing (production/DORMANT.md).
    switches: dict[Literal['asset_stress_test', 'source_reading', 'sample_mode'], bool]
    csrf_token: Annotated[str, StringConstraints(min_length=16, max_length=256)]


def defaults(store: Store, config: Any, pid: str, conn: sqlite3.Connection | None = None) -> dict[str, bool]:
    """What the runtime config declares; without a declaration every step stays on."""
    try:
        declared = json.loads(config.document('execution_policy')).get('owner_switches')
    except (DomainError, KeyError, TypeError, ValueError):
        declared = None
    if not isinstance(declared, dict):
        result = {k: k not in OFF_UNLESS_SET for k in DEFAULTS}
    else:
        result = {k: k not in OFF_UNLESS_SET and declared.get(k, DEFAULTS[k]) is True for k in DEFAULTS}
    result['source_reading'] = _reading_default(store, pid, conn)
    return result


def current(store: Store, config: Any, pid: str, conn: sqlite3.Connection | None = None) -> dict[str, bool]:
    """The film's switches: the owner's last setting, else the runtime config's default."""
    base = defaults(store, config, pid, conn)
    obj = _stored(store, pid, conn)
    if obj is None:
        return base
    stored = obj['body'].get('switches') or {}
    return {k: stored[k] if isinstance(stored.get(k), bool) else base[k] for k in DEFAULTS}


class Switches:
    def __init__(self, store: Store, auth: AuthService, config: Any) -> None:
        self.store, self.auth, self.config = store, auth, config

    def get(self, actor: Principal, pid: str) -> dict[str, Any]:
        with self.store.transaction(write=False) as db:
            self.auth.authorize(actor, pid, 'projects', conn=db)
            return {'switches': current(self.store, self.config, pid, db)}

    def set(self, human: Principal, pid: str, request: SwitchesUpdate, *, origin: str) -> dict[str, Any]:
        """Only the owner's human session changes a switch; the latest setting wins."""
        with self.store.transaction() as db:
            self.auth.authorize(human, pid, 'human-decision', origin=origin, csrf_token=request.csrf_token, conn=db)

            def save(conn: sqlite3.Connection) -> dict[str, Any]:
                merged = {**current(self.store, self.config, pid, conn), **request.switches}
                body = {'switches': merged, 'by': human.actor_id}
                obj = _stored(self.store, pid, conn)
                if obj is None:
                    self.store.create_object(pid, KIND, body, AUTHOR, object_id=object_id(pid), conn=conn)
                else:
                    self.store.append_revision(pid, obj['object_id'], obj['revision'], body, AUTHOR, conn=conn)
                return {'switches': merged}
            return self.store.run_idempotent(f'{pid}:{human.credential_id}:switches', request.idempotency_key,
                                             request.model_dump(), save, conn=db)
