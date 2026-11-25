"""Post-issuance credit positions.

The module is intentionally framework-free: HTTP adapters and SQLite are thin
wrappers around :class:`CreditLedger`.  A position always keeps two numbers:

* ``recomputed_credits`` -- sum of the pairings currently in force;
* ``available_credits`` -- credits usable by the workbench.  When the
  recomputed total is below the training-plan floor, the whole deduction is
  void and the available number is zero.
"""

from __future__ import annotations

import copy
import re
import uuid
from datetime import datetime, timezone
from typing import Any, Iterable

from app.business import DomainIssue
from app.modules.calculate import compute as optimize_matching

SCHEMA = 'credit-position/1'
CERTIFICATE_SCHEMA = 'credit-certificate/1'

# Existing case/review vocabulary is reused where possible.
ACTIVE = 'ACTIVE'
WITHDRAWN = 'WITHDRAWN'
SUPERSEDED = 'SUPERSEDED'
STALE = 'STALE'
READY = 'READY'
SHORTFALL = 'CREDIT_SHORTFALL'
HELD = 'HELD'
INVALID = 'INVALID'

_ID_KEYS = (
    'application_id', 'applicationId', 'app_id', 'appId', 'id',
    'case_id', 'caseId', 'case', 'applicant_id', 'student_id',
)
_ISSUE_KEYS = (
    'issue_number', 'issueNumber', 'issue_no', 'issueNo', 'issuance_id',
    'certificate_number', 'certificate_no', 'credential_number',
    'credential_no', 'voucher_number', 'voucher_no',
)
_RUN_KEYS = ('run_id', 'runId', 'batch_id', 'batchId')
_EXTERNAL_KEYS = (
    'external_course', 'externalCourse', 'external_course_id',
    'externalCourseId', 'source_course', 'sourceCourse', 'source',
    'course_id', 'courseId', 'course', 'withdrawn_course',
    'withdrawnCourse', 'old_external_course', 'oldExternalCourse',
    'old_course', 'oldCourse',
)
_OLD_EXTERNAL_KEYS = (
    'old_external_course', 'oldExternalCourse', 'old_course', 'oldCourse',
    'withdrawn_course', 'withdrawnCourse', 'external_course',
    'externalCourse', 'course', 'course_id',
)
_NEW_EXTERNAL_KEYS = (
    'replacement_course', 'replacementCourse', 'replacement_external_course',
    'replacementExternalCourse', 'new_external_course', 'newExternalCourse',
    'new_course', 'newCourse', 'replacement',
)
_LOCAL_KEYS = (
    'local_course', 'localCourse', 'local_course_id', 'localCourseId',
    'target_course', 'targetCourse', 'target', 'local',
)
_PAIR_KEYS = ('pair_id', 'pairId', 'match_id', 'matchId', 'pair', 'match')
_MIN_KEYS = (
    'minimum_credits', 'minimumCredits', 'required_credits',
    'requiredCredits', 'min_credits', 'minCredits', 'credit_floor',
    'creditFloor', 'floor', 'expected',
)
_CREDIT_KEYS = ('credits', 'credit', 'recognized_credits', 'awarded_credits', 'value')
_PRIORITY_KEYS = ('priority', 'rank', 'approval_rank', 'approval_priority')
_TIME_KEYS = (
    'effective_at', 'effectiveAt', 'effective_time', 'effectiveTime',
    'occurred_at', 'occurredAt', 'event_time', 'eventTime', 'at', 'time',
    'date', 'sequence', 'order', 'order_key',
)
_EVENT_KEYS = ('event_id', 'eventId', 'idempotency_key', 'idempotencyKey', 'request_id', 'requestId')


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec='milliseconds')


def _key(name: Any) -> str:
    return re.sub(r'[_\-\s]+', '', str(name)).lower()


def lookup(data: Any, *names: str, default: Any = None) -> Any:
    """Look up a key while tolerating snake_case, camelCase and aliases."""
    if not isinstance(data, dict):
        return default
    index = {_key(k): v for k, v in data.items()}
    for name in names:
        value = index.get(_key(name))
        if value is not None and value != '':
            return value
    return default


def as_id(value: Any) -> str | None:
    if value is None or value == '':
        return None
    if isinstance(value, bool):
        raise DomainIssue('INVALID_IDENTIFIER')
    if isinstance(value, float) and value.is_integer():
        return str(int(value))
    return str(value).strip()


def as_credits(value: Any, *, required: bool = False) -> Any:
    if value is None or value == '':
        if required:
            raise DomainIssue('CREDITS_REQUIRED')
        return 0
    if isinstance(value, bool):
        raise DomainIssue('INVALID_CREDITS')
    if isinstance(value, str):
        # Reuse the source parser so full-width digits and grouping behave
        # consistently with CSV intake.
        from app.intake.numeric import quantity
        return quantity(value)
    if isinstance(value, (int, float)):
        if not isinstance(value, bool) and (value != value or value in (float('inf'), float('-inf'))):
            raise DomainIssue('INVALID_CREDITS')
        if value < 0:
            raise DomainIssue('INVALID_CREDITS')
        if isinstance(value, float) and value.is_integer():
            return int(value)
        return value
    raise DomainIssue('INVALID_CREDITS')


def time_key(value: Any) -> tuple[int, Any]:
    if value is None or value == '':
        return (3, 0)
    if isinstance(value, bool):
        return (3, 0)
    if isinstance(value, (int, float)):
        return (0, float(value))
    text = str(value).strip()
    if re.fullmatch(r'-?\d+(?:\.\d+)?', text):
        return (0, float(text))
    try:
        parsed = datetime.fromisoformat(text.replace('Z', '+00:00'))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return (1, parsed.timestamp())
    except ValueError:
        return (2, text)


def later_or_equal(candidate: Any, boundary: Any) -> bool:
    left, right = time_key(candidate), time_key(boundary)
    # Missing candidate time means "now"/after the boundary.
    if left[0] == 3:
        return True
    if right[0] == 3:
        return False
    return left >= right


def _pair_identity(external: Any, local: Any) -> str:
    return f'{as_id(external)}::{as_id(local)}'


def normalize_pair(raw: Any = None, **overrides: Any) -> dict[str, Any]:
    data: dict[str, Any] = {}
    if isinstance(raw, dict):
        data.update(raw)
    elif raw is not None:
        # Python callers may pass (external, local, credits).
        if isinstance(raw, (tuple, list)):
            if len(raw) >= 1:
                data['external_course'] = raw[0]
            if len(raw) >= 2:
                data['local_course'] = raw[1]
            if len(raw) >= 3:
                data['credits'] = raw[2]
        else:
            data['external_course'] = raw
    data.update({k: v for k, v in overrides.items() if v is not None})

    external = as_id(lookup(data, *_EXTERNAL_KEYS))
    local = as_id(lookup(data, *_LOCAL_KEYS))
    pair_id = as_id(lookup(data, *_PAIR_KEYS))
    if not external and pair_id and '::' in pair_id:
        external, local_from_id = pair_id.split('::', 1)
        local = local or local_from_id
    if not external or not local:
        raise DomainIssue('PAIR_FIELDS_REQUIRED')

    credits = as_credits(lookup(data, *_CREDIT_KEYS, default=0))
    priority = lookup(data, *_PRIORITY_KEYS, default=0)
    try:
        priority = int(priority)
    except (TypeError, ValueError) as error:
        raise DomainIssue('INVALID_PRIORITY') from error

    explicit_status = lookup(data, 'status', 'pair_status')
    withdrawn = bool(lookup(data, 'withdrawn', 'is_withdrawn', default=False))
    active = lookup(data, 'active', 'is_active', default=not withdrawn)
    if explicit_status == WITHDRAWN or withdrawn:
        status = WITHDRAWN
        active = False
    elif explicit_status and explicit_status not in ('ACTIVE', 'active', READY):
        status = str(explicit_status).upper()
        active = status == ACTIVE
    else:
        status = ACTIVE if active is not False else WITHDRAWN

    return {
        'pair_id': pair_id or _pair_identity(external, local),
        'external_course': external,
        'local_course': local,
        'credits': credits,
        'priority': priority,
        'status': status,
        'active': status == ACTIVE and active is not False,
        'withdrawn': status == WITHDRAWN or withdrawn,
    }


def normalize_pairs(value: Any) -> list[dict[str, Any]]:
    if value is None:
        return []
    if isinstance(value, dict):
        pairs = []
        for key, item in value.items():
            if isinstance(item, dict):
                row = dict(item)
                row.setdefault('external_course', key)
                pairs.append(normalize_pair(row))
            elif isinstance(item, (list, tuple)):
                pairs.append(normalize_pair((key, *item)))
            else:
                # Evidence form: {"1": 2} means external 1 -> local 2.
                pairs.append(normalize_pair({'external_course': key, 'local_course': item}))
        return pairs
    if isinstance(value, Iterable) and not isinstance(value, (str, bytes)):
        return [normalize_pair(item) for item in value]
    raise DomainIssue('INVALID_PAIRS')


def _course_registry(state_data: dict[str, Any], key: str) -> dict[str, Any]:
    raw = state_data.get(key) or {}
    result = {}
    if isinstance(raw, dict):
        for course, info in raw.items():
            course_id = as_id(course)
            if isinstance(info, dict):
                result[course_id] = dict(info)
            else:
                result[course_id] = {'credits': as_credits(info)}
    elif isinstance(raw, list):
        for info in raw:
            if not isinstance(info, dict):
                continue
            course_id = as_id(lookup(info, 'course', 'external_course', *_EXTERNAL_KEYS))
            if course_id:
                result[course_id] = dict(info)
    return result


def issue_application(data: Any = None, **kwargs: Any) -> dict[str, Any]:
    if isinstance(data, CreditLedger):
        return copy.deepcopy(data.state)
    if data is None:
        values = dict(kwargs)
    elif isinstance(data, dict):
        values = {**data, **kwargs}
    elif isinstance(data, (list, tuple)):
        values = {'pairs': data, **kwargs}
    else:
        values = {'application_id': data, **kwargs}

    # Existing normalized state should survive a CreditLedger round-trip.
    if values.get('schema') == SCHEMA and isinstance(values.get('pairs'), list):
        state = copy.deepcopy(values)
        recompute(state)
        return state

    application_id = as_id(lookup(values, *_ID_KEYS)) or f'APP-{uuid.uuid4().hex[:12]}'
    issue_no = as_id(lookup(values, *_ISSUE_KEYS))
    run_id = as_id(lookup(values, *_RUN_KEYS))
    minimum = as_credits(lookup(values, *_MIN_KEYS, default=0), required=True)

    pair_value = lookup(values, 'pairs', 'match_pairs', 'pairings', 'matches', 'matched', 'items')
    pairs = normalize_pairs(pair_value)
    candidate_value = lookup(values, 'candidate_pairs', 'equivalences', 'all_pairs', 'edges', 'candidates')
    candidate_pairs = normalize_pairs(candidate_value) if candidate_value is not None else copy.deepcopy(pairs)
    known_candidate_ids = {p['pair_id'] for p in candidate_pairs}
    for pair in pairs:
        if pair['pair_id'] not in known_candidate_ids:
            candidate_pairs.append(copy.deepcopy(pair))
            known_candidate_ids.add(pair['pair_id'])

    external_courses = _course_registry(values, 'external_courses')
    local_courses = _course_registry(values, 'local_courses')

    issued_credits = lookup(values, 'issued_credits', 'original_credits', 'initial_credits')
    if issued_credits is None:
        issued_credits = sum(p['credits'] for p in pairs if p['active'])
    else:
        issued_credits = as_credits(issued_credits)

    timestamp = lookup(values, 'issued_at', 'created_at', default=now_iso())
    state: dict[str, Any] = {
        'schema': SCHEMA,
        'application_id': application_id,
        'case_id': as_id(lookup(values, 'case_id', 'case', 'caseId')),
        'run_id': run_id,
        'issue_no': issue_no,
        'issue_number': issue_no,
        'minimum_credits': minimum,
        'required_credits': minimum,
        'issued_credits': issued_credits,
        'original_credits': issued_credits,
        'recomputed_credits': issued_credits,
        'available_credits': issued_credits,
        'current_credits': issued_credits,
        'recognized_credits': issued_credits,
        'credits': issued_credits,
        'deduction_valid': True,
        'valid': True,
        'active': True,
        'held': False,
        'restored': False,
        'status': lookup(values, 'status', default=READY),
        'layer': 'APPLICATION',
        'level': 'APPLICATION',
        'scope': 'APPLICATION',
        'pairs': pairs,
        'candidate_pairs': candidate_pairs,
        'external_courses': external_courses,
        'local_courses': local_courses,
        'withdrawals': [],
        'corrections': [],
        'events': [],
        'rejected_events': [],
        'revision': int(lookup(values, 'revision', default=0) or 0),
        'issued_at': timestamp,
        'updated_at': timestamp,
    }
    state['original'] = {
        'application_id': application_id,
        'issue_no': issue_no,
        'minimum_credits': minimum,
        'issued_credits': issued_credits,
        'pairs': copy.deepcopy(pairs),
        'issued_at': timestamp,
    }
    recompute(state)
    state['original']['available_credits'] = issued_credits
    state['original']['deduction_valid'] = issued_credits >= minimum
    return state


def _withdrawal_filters(state: dict[str, Any]):
    withdrawn_external = set()
    withdrawn_pair_ids = set()
    withdrawn_pair_keys = set()
    for item in state.get('withdrawals', []):
        ext = as_id(item.get('external_course'))
        if ext:
            withdrawn_external.add(ext)
        pair_id = as_id(item.get('pair_id'))
        if pair_id:
            withdrawn_pair_ids.add(pair_id)
            if '::' in pair_id:
                withdrawn_pair_keys.add(pair_id)
        local = as_id(item.get('local_course'))
        if ext and local:
            withdrawn_pair_keys.add(_pair_identity(ext, local))
    for pair in state.get('pairs', []):
        if pair.get('withdrawn') or pair.get('status') == WITHDRAWN:
            withdrawn_pair_ids.add(pair.get('pair_id'))
            withdrawn_pair_keys.add(pair.get('pair_id'))
            withdrawn_pair_keys.add(_pair_identity(pair['external_course'], pair['local_course']))
    return withdrawn_external, withdrawn_pair_ids, withdrawn_pair_keys


def recompute(state: dict[str, Any]) -> dict[str, Any]:
    pairs = state.get('pairs', [])
    candidates = state.get('candidate_pairs') or copy.deepcopy(pairs)
    withdrawn_external, withdrawn_pair_ids, withdrawn_pair_keys = _withdrawal_filters(state)

    # Unaffected issued/effective pairings are frozen.  Only the slots freed by
    # withdrawal are reoptimized from registered equivalences/corrections.
    fixed_pairs = [
        p for p in pairs
        if p.get('active') and p.get('status') == ACTIVE and not p.get('withdrawn')
        and p.get('external_course') not in withdrawn_external
        and p.get('pair_id') not in withdrawn_pair_ids
        and p.get('pair_id') not in withdrawn_pair_keys
    ]
    fixed_external = {p['external_course'] for p in fixed_pairs}
    used_local = {p['local_course'] for p in fixed_pairs}
    fixed_credits = sum(as_credits(p.get('credits', 0)) for p in fixed_pairs)

    residual_edges: dict[tuple[Any, Any], tuple[Any, Any]] = {}
    source_capacity: dict[Any, Any] = {}
    target_capacity: dict[Any, Any] = {}
    available_candidates = []
    for pair in candidates:
        ext, local = pair.get('external_course'), pair.get('local_course')
        pair_id = pair.get('pair_id') or _pair_identity(ext, local)
        if not ext or not local:
            continue
        if ext in withdrawn_external or ext in fixed_external or local in used_local:
            continue
        if pair_id in withdrawn_pair_ids or pair_id in withdrawn_pair_keys:
            continue
        if pair.get('withdrawn') or pair.get('status') == WITHDRAWN:
            continue
        credits = as_credits(pair.get('credits', 0))
        if credits <= 0:
            continue
        key = (ext, local)
        value = (credits, int(pair.get('priority', 0) or 0))
        # Duplicate corrections keep the largest registered credit edge.
        if key not in residual_edges or credits > residual_edges[key][0]:
            residual_edges[key] = value
        source_capacity[ext] = max(source_capacity.get(ext, credits), credits)
        target_capacity[local] = max(target_capacity.get(local, credits), credits)
        available_candidates.append(pair)

    residual_value = 0
    residual_matched: dict[Any, Any] = {}
    if residual_edges:
        measure = optimize_matching((source_capacity, target_capacity, residual_edges, 0))
        residual_value = measure.get('value', 0)
        residual_matched = measure.get('evidence', {}).get('matched', {})

    # Promote the residual optimum so workbench and later events see the actual
    # current pairings, including an already-registered alternate equivalence.
    existing_by_key = {(p['external_course'], p['local_course']): p for p in pairs}
    for ext_key, local_value in residual_matched.items():
        ext = as_id(ext_key)
        local = as_id(local_value)
        credits, priority = residual_edges.get((ext, local), residual_edges.get((int(ext) if ext and ext.isdigit() else ext,
                                                                                  int(local) if local and local.isdigit() else local), (0, 0)))
        pair_id = _pair_identity(ext, local)
        pair = existing_by_key.get((ext, local))
        if pair is None:
            pair = {
                'pair_id': pair_id,
                'external_course': ext,
                'local_course': local,
                'credits': credits,
                'priority': priority,
                'status': ACTIVE,
                'active': True,
                'withdrawn': False,
                'promoted_on_recompute': True,
            }
            pairs.append(pair)
            existing_by_key[(ext, local)] = pair
        else:
            pair.update(status=ACTIVE, active=True, withdrawn=False)

    raw = fixed_credits + residual_value
    minimum = as_credits(state.get('minimum_credits', state.get('required_credits', 0)))
    valid = raw >= minimum
    available = raw if valid else 0
    state['recomputed_credits'] = raw
    state['remaining_credits'] = raw
    state['gross_credits'] = raw
    state['residual_recomputed_credits'] = residual_value
    state['total_credits'] = raw
    state['available_credits'] = available
    state['usable_credits'] = available
    state['deduction_credits'] = available
    state['current_available_credits'] = available
    state['current_credits'] = available
    state['recognized_credits'] = available
    state['credits'] = available
    state['deduction_valid'] = valid
    state['valid'] = valid
    state['active'] = valid
    state['void'] = not valid
    state['lapsed'] = not valid
    state['deduction_void'] = not valid
    # A position with a recorded withdrawal/correction history is held at the
    # application layer while its deduction is void.  If the floor is met,
    # unaffected pairings continue without stopping the batch.
    state['held'] = not valid and bool(state.get('withdrawals') or state.get('corrections'))
    state['batch_held'] = False
    state['batch_stopped'] = False
    state['layer'] = 'APPLICATION'
    state['level'] = 'APPLICATION'
    state['scope'] = 'APPLICATION'
    state['hold_layer'] = 'APPLICATION'
    state['stop_layer'] = 'APPLICATION'
    state['stopped_at'] = 'APPLICATION'
    state['layer_name'] = 'application'
    state['status'] = READY if valid else SHORTFALL
    state['state'] = READY if valid else SHORTFALL
    state['application_status'] = 'ACTIVE' if valid else HELD
    state['credit_status'] = 'VALID' if valid else INVALID
    state['deduction_status'] = 'VALID' if valid else INVALID
    active_pairs = [p for p in pairs if p.get('active') and p.get('status') == ACTIVE and not p.get('withdrawn')
                    and p.get('external_course') not in withdrawn_external]
    state['matched'] = {
        p['external_course']: _maybe_int(p['local_course']) for p in active_pairs
    }
    state['active_pair_ids'] = [p['pair_id'] for p in active_pairs]
    state['updated_at'] = now_iso()
    return state


def _maybe_int(value: str) -> Any:
    try:
        number = int(value)
        if str(number) == value:
            return number
    except (TypeError, ValueError):
        pass
    return value


def _event_id(data: dict[str, Any], prefix: str) -> str:
    return as_id(lookup(data, *_EVENT_KEYS)) or f'{prefix}-{uuid.uuid4().hex[:16]}'


def _find_pairs(state: dict[str, Any], external: str | None, local: str | None, pair_id: str | None):
    matches = []
    for pair in state.get('pairs', []):
        if pair_id and pair.get('pair_id') != pair_id:
            continue
        if external and pair.get('external_course') != external:
            continue
        if local and pair.get('local_course') != local:
            continue
        matches.append(pair)
    return matches


def apply_withdrawal(state: dict[str, Any] | 'CreditLedger', data: Any = None, **kwargs: Any) -> dict[str, Any]:
    ledger = state if isinstance(state, CreditLedger) else CreditLedger(state)
    values = dict(data) if isinstance(data, dict) else {}
    if data is not None and not isinstance(data, dict):
        values = {'course': data}
    values.update(kwargs)

    # A combined withdrawal+correction request applies the correction first;
    # the withdrawal below has the final say on the withdrawn course.
    companion = lookup(values, 'correction', 'corrections', 'event')
    if isinstance(companion, dict):
        ledger.state = apply_correction(ledger.state, companion, _allow_withdrawn_replacement=True)
    elif isinstance(companion, list):
        for item in companion:
            ledger.state = apply_correction(ledger.state, item, _allow_withdrawn_replacement=True)
    if lookup(values, 'new_external_course', *_NEW_EXTERNAL_KEYS):
        ledger.state = apply_correction(ledger.state, values, _allow_withdrawn_replacement=True)

    pair_id = as_id(lookup(values, *_PAIR_KEYS))
    external = as_id(lookup(values, *_EXTERNAL_KEYS))
    local = as_id(lookup(values, *_LOCAL_KEYS))
    if pair_id and '::' in pair_id and not external:
        external, maybe_local = pair_id.split('::', 1)
        local = local or maybe_local
    if not external and not pair_id:
        raise DomainIssue('WITHDRAWAL_FIELDS_REQUIRED')

    event_id = _event_id(values, 'WDR')
    effective_at = lookup(values, *_TIME_KEYS, default=now_iso())
    matching = _find_pairs(ledger.state, external, local, pair_id)

    # Also accept a course registered in the source catalogue but not present
    # in an issued pairing.  It has no credit effect.
    affected = []
    for pair in matching:
        pair['status'] = WITHDRAWN
        pair['active'] = False
        pair['withdrawn'] = True
        pair['withdrawn_at'] = effective_at
        pair['withdrawal_id'] = event_id
        affected.append(pair['pair_id'])

    withdrawal = {
        'event_id': event_id,
        'external_course': external,
        'local_course': local,
        'pair_id': pair_id,
        'affected_pair_ids': affected,
        'effective_at': effective_at,
        'submitted_at': lookup(values, 'submitted_at', 'created_at', default=now_iso()),
        'simultaneous': bool(lookup(values, 'simultaneous', default=False)),
    }
    ledger.state.setdefault('withdrawals', []).append(withdrawal)
    ledger.state.setdefault('events', []).append({'type': 'WITHDRAWAL', **withdrawal})
    recompute(ledger.state)
    ledger.state['revision'] = int(ledger.state.get('revision', 0)) + 1
    ledger.state['last_withdrawal'] = withdrawal
    ledger.state['affected_pair_ids'] = affected
    return copy.deepcopy(ledger.state)


def _matching_withdrawal(state: dict[str, Any], external: str | None, local: str | None, pair_id: str | None):
    candidates = state.get('withdrawals', [])
    matches = []
    for item in candidates:
        if pair_id and item.get('pair_id') == pair_id:
            matches.append(item)
            continue
        if external and item.get('external_course') == external:
            if not local or not item.get('local_course') or item.get('local_course') == local:
                matches.append(item)
    return matches


def apply_correction(state: dict[str, Any] | 'CreditLedger', data: Any = None, **kwargs: Any) -> dict[str, Any]:
    ledger = state if isinstance(state, CreditLedger) else CreditLedger(state)
    values = dict(data) if isinstance(data, dict) else {}
    if data is not None and not isinstance(data, dict):
        values = {'external_course': data}
    values.update(kwargs)

    # Combined payloads may nest the withdrawal.  In the simultaneous case the
    # correction is applied first and withdrawal afterwards by the caller.
    nested_withdrawal = lookup(values, 'withdrawal')
    if isinstance(nested_withdrawal, dict) and not kwargs.get('_from_withdrawal'):
        ledger.state = apply_withdrawal(ledger.state, nested_withdrawal)

    pair_id = as_id(lookup(values, *_PAIR_KEYS))
    old_external = as_id(lookup(values, *_OLD_EXTERNAL_KEYS))
    external = as_id(lookup(values, *_EXTERNAL_KEYS))
    replacement = as_id(lookup(values, *_NEW_EXTERNAL_KEYS))
    local = as_id(lookup(values, *_LOCAL_KEYS))

    # Common shape: external_course names the old/withdrawn course and
    # new_external_course/replacement_course names the replacement.
    target_external = old_external or external
    new_external = replacement
    if replacement and not target_external:
        target_external = external

    event_id = _event_id(values, 'COR')
    effective_at = lookup(values, *_TIME_KEYS, default=now_iso())
    matches = _find_pairs(ledger.state, target_external, local, pair_id)
    withdrawals = _matching_withdrawal(ledger.state, target_external, local, pair_id)

    stale_same_course = bool(withdrawals) and not new_external
    if stale_same_course:
        latest = max(withdrawals, key=lambda w: time_key(w.get('effective_at')))
        # An undated correction is treated as arriving after withdrawal; it is
        # still an old/same-course record and cannot resurrect that course.
        is_old = not later_or_equal(effective_at, latest.get('effective_at'))
        event = {
            'event_id': event_id,
            'type': 'CORRECTION',
            'external_course': target_external,
            'local_course': local,
            'pair_id': pair_id,
            'effective_at': effective_at,
            'applied': False,
            'stale': True,
            'reason': 'STALE_CORRECTION' if is_old else 'WITHDRAWN_COURSE',
        }
        ledger.state.setdefault('corrections', []).append(event)
        ledger.state.setdefault('rejected_events', []).append(event)
        ledger.state.setdefault('events', []).append(event)
        ledger.state['last_correction'] = event
        ledger.state['last_correction_applied'] = False
        ledger.state['stale'] = True
        recompute(ledger.state)
        # Rejected records are audited but do not create a new data revision.
        return copy.deepcopy(ledger.state)

    affected: list[str] = []

    # Credit/priority correction on an existing active pairing.
    if not new_external and matches:
        for pair in matches:
            if pair.get('withdrawn') or pair.get('status') == WITHDRAWN:
                continue
            if lookup(values, *_CREDIT_KEYS) is not None:
                pair['credits'] = as_credits(lookup(values, *_CREDIT_KEYS))
            priority = lookup(values, *_PRIORITY_KEYS)
            if priority is not None:
                pair['priority'] = int(priority)
            pair['corrected_at'] = effective_at
            pair['correction_id'] = event_id
            affected.append(pair['pair_id'])

    # Replacement/additional pairings.  A payload may provide a "pairs" list;
    # otherwise construct one pair from the flat fields.
    extra_pairs = lookup(values, 'pairs', 'replacement_pairs', 'new_pairs')
    new_rows = normalize_pairs(extra_pairs) if extra_pairs is not None else []
    if not new_rows and local and (new_external or (target_external and not matches)):
        row = dict(values)
        if new_external:
            row['external_course'] = new_external
        elif target_external:
            row['external_course'] = target_external
        row['local_course'] = local
        new_rows = [normalize_pair(row)]

    existing_ids = {p['pair_id']: p for p in ledger.state['pairs']}
    existing_candidate_ids = {p['pair_id']: p for p in ledger.state.get('candidate_pairs', [])}
    all_withdrawn_external, _, _ = _withdrawal_filters(ledger.state)
    for row in new_rows:
        # A replacement must never silently resurrect the withdrawn course.
        if ((withdrawals and row['external_course'] == target_external and not new_external)
                or row['external_course'] in all_withdrawn_external):
            row['status'] = WITHDRAWN
            row['active'] = False
            row['withdrawn'] = True
        if row['pair_id'] in existing_ids:
            existing = existing_ids[row['pair_id']]
            if not existing.get('withdrawn'):
                existing.update({k: v for k, v in row.items() if k != 'pair_id'})
                affected.append(existing['pair_id'])
        else:
            ledger.state['pairs'].append(row)
            existing_ids[row['pair_id']] = row
            affected.append(row['pair_id'])
        if row['pair_id'] in existing_candidate_ids:
            existing_candidate_ids[row['pair_id']].update(row)
        else:
            ledger.state.setdefault('candidate_pairs', []).append(copy.deepcopy(row))
            existing_candidate_ids[row['pair_id']] = row

    correction = {
        'event_id': event_id,
        'external_course': target_external,
        'new_external_course': new_external,
        'local_course': local,
        'pair_id': pair_id,
        'affected_pair_ids': affected,
        'effective_at': effective_at,
        'applied': True,
        'stale': False,
        'restores': None,
    }
    ledger.state.setdefault('corrections', []).append(correction)
    ledger.state.setdefault('events', []).append({'type': 'CORRECTION', **correction})
    before_valid = ledger.state.get('deduction_valid')
    recompute(ledger.state)
    if not before_valid and ledger.state['deduction_valid']:
        ledger.state['restored'] = True
        ledger.state['restored_at'] = effective_at
        correction['restores'] = 'APPLICATION'
    ledger.state['revision'] = int(ledger.state.get('revision', 0)) + 1
    ledger.state['last_correction'] = correction
    ledger.state['last_correction_applied'] = True
    ledger.state['stale'] = False
    return copy.deepcopy(ledger.state)


def settle(events: Iterable[Any] | None = None, data: Any = None, **kwargs: Any) -> dict[str, Any]:
    """Apply a mixed batch.

    Withdrawals are ordered after corrections at the same logical time so a
    withdrawn course can never count in a simultaneous submission.
    """
    if isinstance(data, CreditLedger):
        ledger = data
        values = kwargs
    elif isinstance(data, dict) and any(k in data for k in ('pairs', 'minimum_credits', *_ID_KEYS)):
        ledger = CreditLedger(data)
        values = kwargs
    else:
        initial = data if isinstance(data, dict) else kwargs
        ledger = CreditLedger(initial)
        values = {}

    event_list = list(events or [])
    if isinstance(values.get('events'), list):
        event_list.extend(values['events'])
    if isinstance(kwargs.get('withdrawal'), dict):
        event_list.append({'type': 'withdrawal', **kwargs['withdrawal']})
    if isinstance(kwargs.get('correction'), dict):
        event_list.append({'type': 'correction', **kwargs['correction']})

    def kind(item: Any) -> str:
        return str(lookup(item if isinstance(item, dict) else {}, 'type', 'kind', 'action', default='')).lower()

    # Explicit old records are ordered by their business time.  Stable tie
    # order puts corrections before withdrawals.
    ordered = sorted(
        enumerate(event_list),
        key=lambda pair: (
            time_key(lookup(pair[1] if isinstance(pair[1], dict) else {}, *_TIME_KEYS))[1]
            if time_key(lookup(pair[1] if isinstance(pair[1], dict) else {}, *_TIME_KEYS))[0] != 3
            else pair[0],
            1 if 'withdraw' in kind(pair[1]) else 0,
            pair[0],
        ),
    )
    for _, event in ordered:
        event_type = kind(event)
        if not isinstance(event, dict):
            continue
        if 'withdraw' in event_type:
            ledger.state = apply_withdrawal(ledger.state, event)
        elif 'correct' in event_type or 'correction' in event_type:
            ledger.state = apply_correction(ledger.state, event)
    recompute(ledger.state)
    return ledger.to_dict()


class CreditLedger:
    """Object facade used by callers that prefer a stateful ledger."""

    def __init__(self, data: Any = None, **kwargs: Any):
        if isinstance(data, CreditLedger):
            self.state = copy.deepcopy(data.state)
        elif isinstance(data, dict) and data.get('schema') == SCHEMA:
            self.state = issue_application(data)
        else:
            self.state = issue_application(data, **kwargs)

    @classmethod
    def issue(cls, data: Any = None, **kwargs: Any) -> 'CreditLedger':
        return cls(issue_application(data, **kwargs))

    def withdraw(self, data: Any = None, **kwargs: Any) -> dict[str, Any]:
        self.state = apply_withdrawal(self.state, data, **kwargs)
        return self.to_dict()

    withdrawal = withdraw
    register_withdrawal = withdraw
    apply_withdrawal = withdraw

    def correct(self, data: Any = None, **kwargs: Any) -> dict[str, Any]:
        self.state = apply_correction(self.state, data, **kwargs)
        return self.to_dict()

    correction = correct
    register_correction = correct
    apply_correction = correct

    def settle(self, events: Iterable[Any] | None = None, **kwargs: Any) -> dict[str, Any]:
        self.state = settle(events, self.state, **kwargs)
        return self.to_dict()

    def recompute(self) -> dict[str, Any]:
        return recompute(self.state)

    @property
    def available_credits(self) -> Any:
        return self.state['available_credits']

    @property
    def current_credits(self) -> Any:
        return self.state['current_credits']

    def to_dict(self) -> dict[str, Any]:
        return copy.deepcopy(self.state)


# Common functional aliases.
create_application = issue_application
create_position = issue_application
issue_position = issue_application
withdraw_course = apply_withdrawal
register_withdrawal = apply_withdrawal
correct_course = apply_correction
register_correction = apply_correction
apply_event = settle
resolve_credits = settle


def available_credits(position: Any = None, **kwargs: Any) -> Any:
    if isinstance(position, CreditLedger):
        return position.available_credits
    if isinstance(position, (int, float)) and not kwargs:
        return position
    if isinstance(position, list):
        data: dict[str, Any] = {'pairs': position, **kwargs}
    elif isinstance(position, dict):
        data = {**position, **kwargs}
    elif position is None:
        data = kwargs
    else:
        return 0

    # Already-computed positions are read directly.  Raw fixtures are resolved
    # so the helper is useful as a small domain API as well.
    if data.get('schema') == SCHEMA and 'available_credits' in data and not kwargs.get('recompute_events'):
        return data['available_credits']
    ledger = CreditLedger.issue(data)
    for item in data.get('withdrawals', []) or []:
        ledger.withdraw(item)
    for item in data.get('corrections', []) or []:
        ledger.correct(item)
    return ledger.available_credits


def current_credits(position: Any = None, **kwargs: Any) -> Any:
    return available_credits(position, **kwargs)


current_available_credits = available_credits
calculate_available_credits = available_credits


def build_position_from_case(case: dict[str, Any], *, run_id: str | None = None,
                             issue_no: str | None = None, issued_at: str | None = None) -> dict[str, Any]:
    ledger_data = case.get('ledger') if isinstance(case.get('ledger'), dict) else {}
    minimum = (case.get('measure') or {}).get('expected')
    pairs = ledger_data.get('pairs')
    if pairs is None:
        matched = ((case.get('measure') or {}).get('evidence') or {}).get('matched') or {}
        pairs = [{'external_course': ext, 'local_course': local} for ext, local in matched.items()]
    data = {
        'application_id': case.get('case'),
        'case_id': case.get('case'),
        'run_id': run_id,
        'issue_no': issue_no,
        'minimum_credits': ledger_data.get('minimum_credits', minimum),
        'pairs': pairs,
        'equivalences': ledger_data.get('equivalences'),
        'external_courses': ledger_data.get('external_courses'),
        'local_courses': ledger_data.get('local_courses'),
        'issued_at': issued_at or now_iso(),
        'status': READY if case.get('status') == READY else case.get('status'),
    }
    return issue_application(data)


def certificate_snapshot(position: dict[str, Any]) -> dict[str, Any]:
    """Return the immutable issuance snapshot for a position."""
    original = position.get('original') or {}
    return {
        'schema': CERTIFICATE_SCHEMA,
        'issue_number': position.get('issue_no') or position.get('issue_number'),
        'issue_no': position.get('issue_no') or position.get('issue_number'),
        'application_id': position.get('application_id'),
        'case_id': position.get('case_id'),
        'run_id': position.get('run_id'),
        'issued_at': position.get('issued_at'),
        'minimum_credits': original.get('minimum_credits', position.get('minimum_credits')),
        'issued_credits': original.get('issued_credits', position.get('issued_credits')),
        'pairs': original.get('pairs', []),
        'original': original,
    }
