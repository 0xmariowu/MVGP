"""The owner's notes on the desk.

The desk's 写一句 lived in the browser only; the agent never saw it unless the owner copied it into the chat.
A note is now a platform record on an exact shot (or film) version, optionally an exact take and a playback
second, which the agent reads with the project context. Only the owner's human session writes one, through the
same session + origin + CSRF check as a pick; no agent or viewer credential can. A note is never deleted: 删除
records a withdrawn revision.
"""
from __future__ import annotations

import sqlite3
from typing import Annotated, Any

from pydantic import Field, StringConstraints

from production.auth import AuthService, Principal
from production.contracts import DomainError, Identifier, Mutation, ObjectRef
from production.store import Store

KIND = 'owner-note'
AUTHOR = 'owner_note_service'


class OwnerNoteRequest(Mutation):
    target: ObjectRef
    take: ObjectRef | None = None
    at_seconds: Annotated[float, Field(ge=0, le=86400)] | None = None
    text: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=2000)]
    csrf_token: Annotated[str, StringConstraints(min_length=16, max_length=256)]


class OwnerNoteWithdraw(Mutation):
    note_id: Identifier
    expected_revision: Annotated[int, Field(ge=1)]
    csrf_token: Annotated[str, StringConstraints(min_length=16, max_length=256)]


def _ref(obj: dict[str, Any]) -> dict[str, Any]:
    return {'object_id': obj['object_id'], 'revision': obj['revision'], 'digest': obj['digest']}


class OwnerNotes:
    def __init__(self, store: Store, auth: AuthService) -> None:
        self.store, self.auth = store, auth

    def _exact(self, pid: str, ref: ObjectRef, kinds: tuple[str, ...], conn: sqlite3.Connection) -> dict[str, Any]:
        obj = self.store.get_object(pid, ref.object_id, revision=ref.revision, conn=conn)
        if ref.digest is None or obj['digest'] != ref.digest or obj['kind'] not in kinds:
            raise DomainError('invalid_input', 'A note names an exact shot, film or take version')
        return obj

    def add(self, human: Principal, pid: str, request: OwnerNoteRequest, *, origin: str) -> dict[str, Any]:
        with self.store.transaction() as db:
            self.auth.authorize(human, pid, 'human-decision', origin=origin, csrf_token=request.csrf_token, conn=db)

            def save(conn: sqlite3.Connection) -> dict[str, Any]:
                target = self._exact(pid, request.target, ('shot', 'media'), conn)
                take = self._exact(pid, request.take, ('media',), conn) if request.take else None
                body = {'target': _ref(target), 'take': _ref(take) if take else None, 'at_seconds': request.at_seconds,
                        'text': request.text, 'withdrawn': False, 'human_actor': human.actor_id,
                        'verified_human_session': True,
                        'dependencies': [_ref(target), *([_ref(take)] if take else [])]}
                return self.store.create_object(pid, KIND, body, AUTHOR, conn=conn)
            return self.store.run_idempotent(f'{pid}:{human.credential_id}:owner-note', request.idempotency_key,
                                             request.model_dump(exclude={'csrf_token'}), save, conn=db)

    def withdraw(self, human: Principal, pid: str, request: OwnerNoteWithdraw, *, origin: str) -> dict[str, Any]:
        with self.store.transaction() as db:
            self.auth.authorize(human, pid, 'human-decision', origin=origin, csrf_token=request.csrf_token, conn=db)

            def save(conn: sqlite3.Connection) -> dict[str, Any]:
                note = self.store.get_object(pid, request.note_id, conn=conn)
                if note['kind'] != KIND or note['author'] != AUTHOR:
                    raise DomainError('invalid_input', 'Only an owner note can be withdrawn')
                if note['revision'] != request.expected_revision:
                    raise DomainError('revision_conflict', 'The note changed', current_revision=note['revision'])
                return self.store.append_revision(pid, note['object_id'], note['revision'],
                                                  {**note['body'], 'withdrawn': True}, AUTHOR, conn=conn)
            return self.store.run_idempotent(f'{pid}:{human.credential_id}:owner-note-withdraw', request.idempotency_key,
                                             request.model_dump(exclude={'csrf_token'}), save, conn=db)
