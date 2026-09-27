"""Read-only reservation bills in each account's native units.

Unknown amounts remain unknown. Stored usage is evidence, never a price estimate.
"""
from __future__ import annotations

import sqlite3
from datetime import UTC, datetime
from typing import Any

from production.store import Store


def _id(value: Any) -> str | None:
    if isinstance(value, dict):
        value = value.get('object_id')
    return value if isinstance(value, str) else None


def _event_objects(value: Any) -> set[str]:
    if isinstance(value, dict):
        found = {value['object_id']} if isinstance(value.get('object_id'), str) else set()
        for child in value.values():
            found.update(_event_objects(child))
        return found
    if isinstance(value, list):
        return set().union(*(_event_objects(child) for child in value))
    return set()


def _usage(body: Any) -> Any:
    if not isinstance(body, dict):
        return None
    if body.get('usage') is not None:
        return body['usage']
    # These are recorded provider values, not dispatch-intent cost policy.
    costs = {key: body[key] for key in ('settled_cost', 'actual_cost', 'cost')
             if body.get(key) is not None}
    for key in ('execution', 'result'):
        nested = _usage(body.get(key))
        if nested is not None:
            return nested
    return costs or None


def bill(store: Store, project_id: str, *, conn: sqlite3.Connection | None = None) -> dict[str, Any]:
    """Project bill from one snapshot; caller connections must be in a transaction.

    Totals include empty envelopes. ``reserved_max`` is the held + unknown
    maximum, with separate state subtotals; ``actual`` sums only settled rows.
    ``reserved`` and ``spent`` are copied from the budget ledger. No conversion
    or inference from usage is performed. Rows without a target event sort last.
    """
    with store._using(conn, write=False) as db:
        budgets = store.list_budgets(project_id, conn=db)
        totals = {b['budget_key']: {
            'unit': b['unit'], 'ceiling': b['ceiling'],
            'count': {'held': 0, 'unknown': 0, 'settled': 0},
            'held_reserved_max': 0, 'unknown_reserved_max': 0,
            'reserved_max': 0, 'actual': 0, 'reserved': b['reserved'], 'spent': b['spent'],
        } for b in budgets}
        times: dict[str, str] = {}
        for event in store.events(project_id, conn=db):
            timestamp = datetime.fromisoformat(event['created_at']).astimezone(UTC).isoformat().replace('+00:00', 'Z')
            for oid in _event_objects(event['body']):
                times[oid] = min(times.get(oid, timestamp), timestamp)

        jobs = store.list_objects(project_id, kind='job', conn=db)
        jobs_by_intent = {_id(j['body'].get('intent')): j for j in jobs}
        batches = store.list_objects(project_id, kind='batch', conn=db)
        batch_by_job = {_id(child.get('job')): batch['object_id']
                        for batch in batches for child in batch['body'].get('children', [])}

        def get(ref: Any) -> dict[str, Any] | None:
            oid = _id(ref)
            if oid is None:
                return None
            revision = ref.get('revision') if isinstance(ref, dict) else None
            return store.get_object(project_id, oid, revision=revision, conn=db)

        rows = []
        for reservation in db.execute('SELECT * FROM reservations WHERE project_id=?', (project_id,)):
            oid = reservation['object_id']
            target = get(oid)
            kind = target['kind'] if target else None
            body = target['body'] if target and isinstance(target['body'], dict) else {}
            description = [kind or 'Unassigned reservation']
            evidence = [body] if kind != 'dispatch-intent' else []
            if kind in ('job', 'dispatch-intent'):
                job = target if kind == 'job' else jobs_by_intent.get(oid)
                job_body = job['body'] if job else {}
                intent = get(job_body.get('intent')) if kind == 'job' else target
                details = {**(intent['body'] if intent else {}), **job_body}
                description = ['Job']
                for key in ('operation', 'purpose', 'task'):
                    if details.get(key) is not None:
                        description.append(f'{key}: {details[key]}')
                for key in ('candidate', 'shot', 'target', 'batch'):
                    if _id(details.get(key)):
                        description.append(f'{key}: {_id(details[key])}')
                if job and not details.get('batch') and job['object_id'] in batch_by_job:
                    description.append(f"batch: {batch_by_job[job['object_id']]}")
                if details.get('take_index') is not None:
                    description.append(f"take: {details['take_index']}")
                remote = details.get('remote_job_id', details.get('provider_job_id'))
                if remote is not None:
                    description.append(f'provider job: {remote}')
                evidence.append(job_body)
                receipt = get(job_body.get('last_receipt'))
                if receipt:
                    evidence.append(receipt['body'])
            elif kind == 'review-task':
                for key in ('gate', 'purpose', 'role', 'route_profile_id'):
                    if body.get(key) is not None:
                        description.append(f'{key}: {body[key]}')
            elif kind == 'batch' and isinstance(body.get('children'), list):
                description.append(f"{len(body['children'])} children")
            for key in ('receipt_id', 'result', 'result_id'):
                result = get(body.get(key))
                if result:
                    evidence.append(result['body'])
            usage = next((value for item in evidence if (value := _usage(item)) is not None), None)
            state = reservation['state']
            actual = reservation['actual'] if state == 'settled' else None
            total = totals[reservation['budget_key']]
            total['count'][state] += 1
            if state in ('held', 'unknown'):
                total[f'{state}_reserved_max'] += reservation['amount']
                total['reserved_max'] += reservation['amount']
            elif actual is not None:
                total['actual'] += actual
            rows.append({
                'reservation_id': reservation['reservation_id'], 'time': times.get(oid),
                'what': '; '.join(description), 'target': {'object_id': oid, 'kind': kind},
                'budget_key': reservation['budget_key'], 'unit': total['unit'],
                'reserved_max': reservation['amount'], 'actual': actual, 'usage': usage, 'state': state,
            })
        rows.sort(key=lambda row: (row['time'] is None, row['time'] or '', row['reservation_id']))
        return {'rows': rows, 'totals': totals}


# what each paid call was, in the owner's words, and who was paid.
WHAT = {'submit': '样片', 'complete-draft': '1080p 正片', 'observe': 'Gemini 看原片', 'render-cut': '合成片'}
IMAGE_JOBS = ('apilio_', 'gpt_image', 'nano_banana')


def _provider(operation: str, job_type: str) -> str:
    if operation == 'observe':
        return 'gemini'
    if operation == 'render-cut':
        return 'local'
    if job_type.startswith('fal_'):
        return 'fal'
    if job_type.startswith('apilio_'):
        return 'apilio'
    return 'higgsfield'


def ledger(store: Store, project_id: str, *, conn: sqlite3.Connection | None = None) -> dict[str, Any]:
    """Every paid call of one project: time, the shot or asset it served, what it was
    (素材图 / 样片 / 1080p 正片 / Gemini 看原片 / 合成片), provider, the provider's request id, and the platform's own
    amount in the account's unit — held (reserved, not settled), settled (what was charged) or unknown (the
    provider's answer was lost; the hold stays until the operator settles it). Read-only; nothing is converted."""
    with store._using(conn, write=False) as db:
        units = {b['budget_key']: b['unit'] for b in store.list_budgets(project_id, conn=db)}
        reservations = {r['reservation_id']: dict(r) for r in db.execute('SELECT * FROM reservations WHERE project_id=?', (project_id,))}
        created = dict(db.execute("SELECT object_id, created_at FROM revisions WHERE project_id=? AND revision=1", (project_id,)).fetchall())
        jobs = {_id(j['body'].get('intent')): j for j in store.list_objects(project_id, kind='job', conn=db)}

        def label(ref: Any, depth: int = 0) -> tuple[str | None, str | None]:
            """(shot label or asset tag, kind) of the object a paid call served, through candidates and draft takes."""
            oid = _id(ref)
            if oid is None or depth > 4:
                return None, None
            try:
                obj = store.get_object(project_id, oid, conn=db)
            except Exception:  # noqa: BLE001 -- a missing record leaves the row unlabelled, never breaks the ledger
                return None, None
            content = obj['body'].get('content') if isinstance(obj['body'], dict) else None
            if obj['kind'] == 'shot':
                return (content or {}).get('shot') if isinstance(content, dict) else None, 'shot'
            if obj['kind'] == 'asset':
                return (content or {}).get('tag') if isinstance(content, dict) else None, 'asset'
            if obj['kind'] == 'candidate':
                return label(obj['body'].get('target'), depth + 1)
            if obj['kind'] == 'dispatch-intent':
                return label(obj['body'].get('candidate') or obj['body'].get('target'), depth + 1)
            if obj['kind'] == 'media':
                for dep in obj['body'].get('dependencies') or []:
                    found = label(dep, depth + 1)
                    if found[0] is not None:
                        return found
                return None, 'media'
            return None, obj['kind']

        rows: list[dict[str, Any]] = []
        totals: dict[str, dict[str, Any]] = {}
        for intent in store.list_objects(project_id, kind='dispatch-intent', conn=db):
            if intent['author'] != 'submission_service':
                continue
            body = intent['body']
            operation, job_type = str(body.get('operation') or ''), str((body.get('request') or {}).get('job_type') or '')
            job = jobs.get(intent['object_id'])
            job_body = job['body'] if job else {}
            name, kind = label(body.get('candidate') or body.get('target'))
            what = '素材图' if operation == 'submit' and job_type.startswith(IMAGE_JOBS) else WHAT.get(operation, operation)
            reservation = reservations.get(job_body.get('reservation_id') or '')
            key = (reservation or {}).get('budget_key') or (body.get('cost') or {}).get('budget_key')
            state = (reservation or {}).get('state') or ('local' if (body.get('cost') or {}).get('mode') == 'local' else None)
            amount = None if reservation is None else reservation['actual'] if state == 'settled' else reservation['amount']
            unit = units.get(key) if key else None
            rows.append({'time': created.get(intent['object_id']), 'shot': name if kind == 'shot' else None,
                         'asset': name if kind == 'asset' else None, 'what': what, 'provider': _provider(operation, job_type),
                         'request_id': job_body.get('remote_job_id'), 'job': job['object_id'] if job else None,
                         'job_state': job_body.get('state'), 'attempt': body.get('attempt'),
                         'state': state, 'amount': amount, 'unit': unit, 'budget_key': key})
            if key and state in ('held', 'settled', 'unknown') and isinstance(amount, int):
                total = totals.setdefault(key, {'unit': unit, 'held': 0, 'settled': 0, 'unknown': 0})
                total[state] += amount
        rows.sort(key=lambda r: (r['time'] is None, r['time'] or '', r['job'] or ''))
        return {'project_id': project_id, 'rows': rows, 'totals': totals}


FAL_ENDPOINTS = {'bytedance/seedance-2.5/reference-to-video': '样片', 'bytedance/seedance-2.5/text-to-video': '样片',
                 'bytedance/seedance-2.5/draft/complete': '1080p 正片'}


def reconcile(store: Store, project_ids: list[str], day: str, ledger_dir: Any, *, quota_per_yuan: int = 500000,
              conn: sqlite3.Connection | None = None) -> dict[str, Any]:
    """The platform's settled totals for one UTC day next to the providers' own records.
    Differences are listed, never corrected. fal: the platform's usd_micro against fal's cost_total in USD, by what
    it was. apilio: the platform's apilio quota converted to 人民币分 (quota × 100 / quota_per_yuan, the pinned
    `quota_per_lightning`; this conversion is our reading of apilio's billing, not a documented rate) against the
    difference of two running-total snapshots (the day's and the day before's). A provider account also serves
    other tools, so its own total may be larger than the platform's."""
    import datetime as dt
    import json
    from pathlib import Path
    folder = Path(ledger_dir)
    def snapshot(name: str) -> dict[str, Any] | None:
        path = folder / name
        return json.loads(path.read_text()) if path.exists() else None
    platform: dict[str, dict[str, Any]] = {'fal': {'settled': 0, 'held': 0, 'unknown': 0, 'by_what': {}},
                                           'apilio': {'settled': 0, 'held': 0, 'unknown': 0, 'by_what': {}}}
    for pid in project_ids:
        for row in ledger(store, pid, conn=conn)['rows']:
            provider = row['provider'] if row['provider'] in platform else ('apilio' if row['unit'] == 'apilio_quota' else None)
            if provider is None or not (row['time'] or '').startswith(day) or row['state'] not in ('settled', 'held', 'unknown'):
                continue
            amount = row['amount'] or 0
            platform[provider][row['state']] += amount
            if row['state'] == 'settled':
                platform[provider]['by_what'][row['what']] = platform[provider]['by_what'].get(row['what'], 0) + amount
    report: dict[str, Any] = {'day': day, 'providers': {}}
    fal = snapshot(f'fal-{day}.json')
    fal_side: dict[str, Any] = {'unit': 'usd_micro', 'platform_settled': platform['fal']['settled'],
                                'platform_held': platform['fal']['held'], 'platform_unknown': platform['fal']['unknown']}
    if fal is None or 'error' in fal:
        fal_side['note'] = f"no fal record for {day}" if fal is None else f"fal record error: {fal['error']}"
    else:
        by_what: dict[str, int] = {}
        for result in fal.get('results') or []:
            what = FAL_ENDPOINTS.get(str(result.get('endpoint_id')), str(result.get('endpoint_id')))
            by_what[what] = by_what.get(what, 0) + round(float(result.get('cost_total') or 0) * 1_000_000)
        provider_total = sum(by_what.values())
        fal_side.update(provider=provider_total, difference=provider_total - platform['fal']['settled'],
                        by_what={k: {'platform': platform['fal']['by_what'].get(k, 0), 'provider': by_what.get(k, 0)}
                                 for k in sorted({*by_what, *platform['fal']['by_what']})},
                        complete=bool(fal.get('complete')))
    report['providers']['fal'] = fal_side
    before = (dt.date.fromisoformat(day) - dt.timedelta(days=1)).isoformat()
    today, yesterday = snapshot(f'apilio-{day}.json'), snapshot(f'apilio-{before}.json')
    to_fen = 100 / quota_per_yuan
    apilio_side: dict[str, Any] = {'unit': '人民币分', 'conversion': f'quota × 100 / {quota_per_yuan} (our reading, not documented)',
                                   'platform_settled': round(platform['apilio']['settled'] * to_fen, 2),
                                   'platform_held': round(platform['apilio']['held'] * to_fen, 2),
                                   'platform_unknown': round(platform['apilio']['unknown'] * to_fen, 2)}
    if not today or not yesterday or 'error' in today or 'error' in yesterday:
        apilio_side['note'] = f'needs the running total of {before} and of {day}; each day has one snapshot'
    else:
        spent = round(float(today['total_usage']) - float(yesterday['total_usage']), 2)
        apilio_side.update(provider=spent, difference=round(spent - apilio_side['platform_settled'], 2))
    report['providers']['apilio'] = apilio_side
    report['providers']['higgsfield'] = _higgsfield_side(store, project_ids, day, snapshot(f'higgsfield-{day}.json'), conn)
    return report


HF_MATCH_BEFORE, HF_MATCH_AFTER = 60, 1800  # seconds a Higgsfield spend may sit before / after the take's dispatch intent


def _higgsfield_side(store: Store, project_ids: list[str], day: str, record: dict[str, Any] | None,
                     conn: sqlite3.Connection | None) -> dict[str, Any]:
    """the platform's Higgsfield credits for one UTC day beside Higgsfield's own transactions.
    Higgsfield names no job in a transaction, so each settled take is matched to one unmatched spend of the same credits
    from HF_MATCH_BEFORE seconds before to HF_MATCH_AFTER after its dispatch intent, in time order. Spends left over are
    the account's other use (the owner's web work), listed apart and never counted as the platform's."""
    import datetime as dt

    def when(value: Any) -> dt.datetime | None:
        try:
            return dt.datetime.fromisoformat(str(value).replace('Z', '+00:00'))
        except ValueError:
            return None
    takes = [row for pid in project_ids for row in ledger(store, pid, conn=conn)['rows']
             if row['provider'] == 'higgsfield' and (row['time'] or '').startswith(day)
             and row['state'] in ('settled', 'held', 'unknown')]
    side: dict[str, Any] = {'unit': 'hf_credit', **{f'platform_{state}': sum(r['amount'] or 0 for r in takes if r['state'] == state)
                                                     for state in ('settled', 'held', 'unknown')}}
    if record is None or 'error' in record:
        side['note'] = f'no Higgsfield record for {day}' if record is None else f"Higgsfield record error: {record['error']}"
        return side
    spends = sorted(({'time': t.get('created_at'), 'credits': -t['credits'], 'model': t.get('display_name')}
                     for t in record.get('transactions') or []
                     if t.get('action') == 'spend' and isinstance(t.get('credits'), (int, float)) and t['credits'] < 0),
                    key=lambda t: str(t['time']))
    others = [t for t in record.get('transactions') or [] if t.get('action') != 'spend']
    used: set[int] = set()
    unmatched = []
    for take in sorted((r for r in takes if r['state'] == 'settled'), key=lambda r: r['time'] or ''):
        start = when(take['time'])
        hit = next((i for i, spend in enumerate(spends) if i not in used and start is not None
                    and spend['credits'] == take['amount'] and (moment := when(spend['time'])) is not None
                    and -HF_MATCH_BEFORE <= (moment - start).total_seconds() <= HF_MATCH_AFTER), None)
        if hit is None:
            unmatched.append({'time': take['time'], 'credits': take['amount'], 'shot': take.get('shot'), 'project_id': take.get('project_id')})
        else:
            used.add(hit)
    not_ours = [spend for i, spend in enumerate(spends) if i not in used]
    provider = sum(t['credits'] for t in spends)
    side.update(provider=provider, matched=len(used), platform_without_spend=unmatched,
                not_the_platforms=not_ours, not_the_platforms_total=sum(t['credits'] for t in not_ours),
                other_actions=[{k: t.get(k) for k in ('action', 'created_at', 'credits', 'display_name')} for t in others],
                difference=provider - sum(t['credits'] for t in not_ours) - side['platform_settled'],
                complete=bool(record.get('complete')))
    if record.get('balance'):
        side['balance'] = record['balance']
    return side


def format_table(report: dict[str, Any]) -> str:
    """Plain aligned text; preserve nulls and keep unlike units separate."""
    import json

    def table(headers: list[str], records: list[list[Any]]) -> str:
        def cell(value: Any) -> str:
            value = 'null' if value is None else json.dumps(value, sort_keys=True) if isinstance(value, dict | list) else str(value)
            return ' '.join(value.split())

        lines = [headers, *[[cell(value) for value in row] for row in records]]
        widths = [max(len(row[i]) for row in lines) for i in range(len(headers))]
        return '\n'.join('  '.join(value.ljust(width) for value, width in zip(row, widths)).rstrip() for row in lines)

    rows = table(['Reservation', 'Time (UTC)', 'What', 'Target', 'Budget', 'Unit', 'Reserved max', 'Actual', 'Usage', 'State'],
                 [[r['reservation_id'], r['time'], r['what'], r['target']['object_id'], r['budget_key'],
                   r['unit'], r['reserved_max'], r['actual'], r['usage'], r['state']] for r in report['rows']])
    totals = table(['Budget', 'Unit', 'Ceiling', 'Held', 'Unknown', 'Settled', 'Held max', 'Unknown max',
                    'Unsettled max', 'Actual', 'Ledger reserved', 'Ledger spent'],
                   [[key, t['unit'], t['ceiling'], t['count']['held'], t['count']['unknown'], t['count']['settled'],
                     t['held_reserved_max'], t['unknown_reserved_max'], t['reserved_max'], t['actual'],
                     t['reserved'], t['spent']] for key, t in report['totals'].items()])
    return f'{rows}\n\nTotals\n{totals}'
