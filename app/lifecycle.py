"""Application-level withdrawal/correction use cases and persistence glue."""

from __future__ import annotations

import json
from typing import Any

from app import credits
from app.business import DomainIssue
from app.evidence.render import artifact as evidence_artifact


def _identifier(data: dict[str, Any], *names: str) -> str | None:
    return credits.as_id(credits.lookup(data, *names))


def ensure_run_positions(store, run: dict[str, Any]) -> list[dict[str, Any]]:
    run_id = run['id']
    existing = store.list_positions(run_id)
    if existing:
        return existing

    now = credits.now_iso()
    positions = []
    for case in run.get('report', {}).get('cases', []):
        if not isinstance(case, dict) or not case.get('case'):
            continue
        issue_no = f"CERT-{case['case']}-{run_id[:12].upper()}"
        position = credits.build_position_from_case(
            case, run_id=run_id, issue_no=issue_no,
            issued_at=run.get('reviewed') or run['report'].get('manifest', {}).get('created') or now,
        )
        store.upsert_position(position)
        positions.append(store.load_position(position['uid']))
    return positions


def issue_certificates(store, run: dict[str, Any], now: str | None = None) -> dict[str, Any]:
    """Snapshot the issued evidence. Later credit events never overwrite it."""
    now = now or credits.now_iso()
    run_id = run['id']
    issued_at = run.get('reviewed') or now
    packet = evidence_artifact(run)

    # The run id itself is accepted as the original issuance number.  A short
    # human-readable alias is stored as another key to the same bytes.
    store.save_certificate(
        run_id, packet, run_id=run_id, application_id='*', scope='RUN',
        snapshot={'schema': 'review-evidence/1', 'issue_number': run_id}, created=issued_at,
    )
    short_no = f'ISSUE-{run_id[:12].upper()}'
    store.save_certificate(
        short_no, packet, run_id=run_id, application_id='*', scope='RUN',
        snapshot={'schema': 'review-evidence/1', 'issue_number': short_no}, created=issued_at,
    )

    positions = ensure_run_positions(store, run)
    for position in positions:
        issue_no = position.get('issue_no')
        if not issue_no:
            continue
        snapshot = credits.certificate_snapshot(position)
        body = (json.dumps(snapshot, ensure_ascii=False, sort_keys=True, indent=2) + '\n').encode()
        store.save_certificate(
            issue_no, body, run_id=run_id,
            application_id=position.get('application_id'), scope='APPLICATION',
            snapshot=snapshot, created=issued_at,
        )
        composite = store._uid(position)
        if composite and composite != issue_no:
            store.save_certificate(
                composite, body, run_id=run_id,
                application_id=position.get('application_id'), scope='APPLICATION',
                snapshot=snapshot, created=issued_at,
            )
    return {'run_id': run_id, 'batch_issue_no': run_id, 'positions': positions}


def _refresh_run(store, run_id: str | None) -> dict[str, Any] | None:
    if not run_id:
        return None
    try:
        run = store.load(run_id)
    except DomainIssue:
        return None
    ensure_run_positions(store, run)
    return run


def resolve_position(store, data: dict[str, Any]) -> dict[str, Any]:
    identifier = _identifier(data, *_IDENTIFIER_KEYS)
    run_id = _identifier(data, 'run_id', 'runId', 'batch_id', 'batchId')
    if not identifier:
        identifier = run_id

    if identifier:
        try:
            return store.load_position(identifier)
        except DomainIssue as error:
            if error.code != 'APPLICATION_NOT_FOUND':
                raise

    # A business case id can be resolved through its run report.
    case_id = _identifier(data, 'case_id', 'caseId', 'case')
    if run_id:
        run = _refresh_run(store, run_id)
        if run is not None:
            if case_id:
                uid = f'{run_id}:{case_id}'
                return store.load_position(uid)
            if identifier and identifier != run_id:
                return store.load_position(identifier)
            positions = store.list_positions(run_id)
            if len(positions) == 1:
                return positions[0]

    if credits.lookup(data, 'pairs', 'minimum_credits', *_MIN_KEYS) is not None:
        return create_application(store, data)

    raise DomainIssue('APPLICATION_NOT_FOUND')


_IDENTIFIER_KEYS = credits._ID_KEYS + credits._ISSUE_KEYS + ('uid', 'position_id')
_MIN_KEYS = credits._MIN_KEYS


def create_application(store, data: dict[str, Any]) -> dict[str, Any]:
    position = credits.issue_application(data)
    if not position.get('issue_no'):
        supplied = credits.as_id(credits.lookup(data, *credits._ISSUE_KEYS))
        position['issue_no'] = supplied or f"ISSUE-{position['application_id']}"
        position['issue_number'] = position['issue_no']
    saved = store.upsert_position(position)
    snapshot = credits.certificate_snapshot(saved)
    body = (json.dumps(snapshot, ensure_ascii=False, sort_keys=True, indent=2) + '\n').encode()
    store.save_certificate(
        saved['issue_no'], body,
        run_id=saved.get('run_id'), application_id=saved.get('application_id'),
        scope='APPLICATION', snapshot=snapshot, created=saved.get('issued_at') or credits.now_iso(),
    )
    saved['certificate_url'] = f"/api/certificates/{saved['issue_no']}/download"
    saved['download_url'] = saved['certificate_url']
    return saved


def _check_position_revision(position: dict[str, Any], data: dict[str, Any]) -> None:
    expected = credits.lookup(data, 'expected_revision', 'position_revision', 'credit_revision')
    if expected is None:
        return
    if int(expected) != int(position.get('revision', 0)):
        raise DomainIssue('STALE_POSITION')


def _apply_event_atomic(store, position: dict[str, Any], data: dict[str, Any], apply_fn,
                        last_key: str, event_type: str) -> dict[str, Any]:
    """Read-modify-write a position under a SQLite write transaction."""
    uid = position['uid']
    supplied_event_id = credits.as_id(credits.lookup(data, *_EVENT_KEYS))
    with store.connect() as db:
        db.execute('BEGIN IMMEDIATE')
        row = db.execute('SELECT state FROM credit_positions WHERE uid=?', (uid,)).fetchone()
        current = json.loads(row['state']) if row is not None else position
        if supplied_event_id and any(e.get('event_id') == supplied_event_id for e in current.get('events', [])):
            current['idempotent_replay'] = True
            return current

        _check_position_revision(current, data)
        updated = apply_fn(current, data)
        issue_no = updated.get('issue_no') or updated.get('issue_number')
        db.execute('''
            UPDATE credit_positions
            SET application_id=?, case_id=?, run_id=?, issue_no=?, revision=?, state=?, updated=?
            WHERE uid=?
        ''', (
            updated.get('application_id'), updated.get('case_id'), updated.get('run_id'),
            issue_no, int(updated.get('revision', 0)),
            json.dumps(updated, ensure_ascii=False), updated.get('updated_at'), uid,
        ))
        last = updated.get(last_key) or {}
        event_id = last.get('event_id') or supplied_event_id
        db.execute('''
            INSERT OR IGNORE INTO credit_events
              (event_id,application_uid,application_id,type,payload,effective_at,created_at)
            VALUES (?,?,?,?,?,?,?)
        ''', (
            event_id, uid, updated.get('application_id'), event_type,
            json.dumps(data, ensure_ascii=False), last.get('effective_at'), credits.now_iso(),
        ))
    return store.load_position(uid)


def withdraw(store, data: dict[str, Any]) -> dict[str, Any]:
    data = dict(data or {})
    run_id = _identifier(data, 'run_id', 'batch_id')
    application = _identifier(data, *credits._ID_KEYS, *credits._ISSUE_KEYS)
    case_id = _identifier(data, 'case_id', 'case')

    # Run/batch-level event without one application: fan out, but retain the
    # application as the hold/decision layer.
    if run_id and not (application or case_id or credits.lookup(data, 'application_id')):
        return withdraw_for_run(store, run_id, data)

    position = resolve_position(store, data)
    event_id = credits.as_id(credits.lookup(data, *_EVENT_KEYS))
    if event_id and any(e.get('event_id') == event_id for e in position.get('events', [])):
        position['idempotent_replay'] = True
        return position
    return _apply_event_atomic(store, position, data, credits.apply_withdrawal,
                               'last_withdrawal', 'WITHDRAWAL')


_EVENT_KEYS = credits._EVENT_KEYS


def withdraw_for_run(store, run_id: str, data: dict[str, Any]) -> dict[str, Any]:
    run = store.load(run_id)
    positions = ensure_run_positions(store, run)
    external = credits.as_id(credits.lookup(data, *_EXTERNAL_KEYS))
    local = credits.as_id(credits.lookup(data, *_LOCAL_KEYS))
    affected, untouched = [], []
    updated_items = []

    for position in positions:
        knows_course = (
            any(p.get('external_course') == external for p in position.get('pairs', []))
            or external in (position.get('external_courses') or {})
        )
        if external and not knows_course:
            untouched.append(position['application_id'])
            continue
        updated = credits.apply_withdrawal(position, data)
        store.upsert_position(updated)
        loaded = store.load_position(updated['uid'])
        updated_items.append(loaded)
        affected.append(loaded['application_id'])
        last = updated.get('last_withdrawal') or {}
        store.save_event(
            last.get('event_id'), updated['uid'], updated['application_id'],
            'WITHDRAWAL', data, last.get('effective_at'),
        )

    return {
        'scope': 'RUN',
        'layer': 'APPLICATION',
        'level': 'APPLICATION',
        'run_id': run_id,
        'batch_held': False,
        'batch_stopped': False,
        'affected_application_ids': affected,
        'unaffected_application_ids': untouched,
        'affected': updated_items,
        'items': updated_items,
        'applications': store.list_positions(run_id),
    }


_EXTERNAL_KEYS = credits._EXTERNAL_KEYS
_LOCAL_KEYS = credits._LOCAL_KEYS


def correct(store, data: dict[str, Any]) -> dict[str, Any]:
    data = dict(data or {})
    position = resolve_position(store, data)
    event_id = credits.as_id(credits.lookup(data, *_EVENT_KEYS))
    if event_id and any(e.get('event_id') == event_id for e in position.get('events', [])):
        position['idempotent_replay'] = True
        return position
    return _apply_event_atomic(store, position, data, credits.apply_correction,
                               'last_correction', 'CORRECTION')


def settle(store, data: dict[str, Any]) -> dict[str, Any]:
    data = dict(data or {})
    position = resolve_position(store, data)
    events = data.get('events') if isinstance(data.get('events'), list) else []
    result = credits.settle(events, position, **{k: v for k, v in data.items() if k in ('withdrawal', 'correction')})
    store.upsert_position(result)
    for event in result.get('events', []):
        store.save_event(
            event.get('event_id'), result['uid'], result['application_id'],
            event.get('type', 'EVENT'), event, event.get('effective_at'),
        )
    return store.load_position(result['uid'])


def workbench(store, identifier: str | None = None, filters: dict[str, Any] | None = None) -> dict[str, Any]:
    filters = filters or {}
    if identifier or credits.lookup(filters, *_IDENTIFIER_KEYS):
        key = identifier or credits.lookup(filters, *_IDENTIFIER_KEYS)
        position = store.load_position(key)
        return {
            'item': position,
            'application': position,
            'items': [position],
            'applications': [position],
            'credits': {position['application_id']: position['available_credits']},
            'available_credits': position['available_credits'],
            'current_credits': position['current_credits'],
        }

    run_id = credits.lookup(filters, 'run_id', 'batch_id')
    items = store.list_positions(run_id)
    if run_id and not items:
        try:
            run = store.load(run_id)
        except DomainIssue:
            run = None
        if run and run.get('status') == 'APPROVED':
            items = ensure_run_positions(store, run)
    elif not run_id:
        # Upgrade path for databases created before post-issuance positions.
        for summary in store.list():
            if summary.get('status') != 'APPROVED':
                continue
            try:
                run = store.load(summary['id'])
            except DomainIssue:
                continue
            ensure_run_positions(store, run)
        items = store.list_positions()
    return {
        'items': items,
        'applications': items,
        'count': len(items),
        'credits': {item['application_id']: item['available_credits'] for item in items},
        'available_credits': {item['application_id']: item['available_credits'] for item in items},
        'total_available_credits': sum(item['available_credits'] for item in items),
    }


def certificate_response(store, issue_no: str) -> dict[str, Any]:
    try:
        return store.load_certificate(issue_no)
    except DomainIssue as error:
        if error.code != 'CERTIFICATE_NOT_FOUND':
            raise
        # Backfill runs approved before the certificate table existed.  The run
        # row itself is immutable after APPROVED, so this still freezes the
        # original stored evidence rather than recomputing it.
        try:
            run = store.load(issue_no)
        except DomainIssue:
            raise error
        if run.get('status') != 'APPROVED':
            raise error
        issue_certificates(store, run)
        return store.load_certificate(issue_no)
