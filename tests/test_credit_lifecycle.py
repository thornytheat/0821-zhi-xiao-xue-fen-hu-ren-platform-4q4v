from app.credits import CreditLedger
from app.lifecycle import correct, withdraw
from app.review.service import execute, review


def approved_run(store, clean):
    run = execute(store, clean)
    return review(store, run['id'], {
        'revision': 0,
        'status': 'APPROVED',
        'note': '培养方案与配对均已复核',
    })


def test_withdrawal_voids_only_application_below_floor(store, clean):
    run = approved_run(store, clean)
    assert run['status'] == 'APPROVED'

    affected = withdraw(store, {
        'run_id': run['id'], 'application_id': '0001', 'external_course': '1',
    })
    assert affected['recomputed_credits'] == 4
    assert affected['available_credits'] == 0
    assert affected['deduction_valid'] is False
    assert affected['layer'] == 'APPLICATION'

    other = store.load_position(f"{run['id']}:0002")
    assert other['available_credits'] == 3
    assert other['deduction_valid'] is True
    assert store.load(run['id'])['status'] == 'APPROVED'


def test_unwithdrawn_pairings_remain_when_floor_still_met(store, clean):
    run = approved_run(store, clean)
    result = withdraw(store, {
        'run_id': run['id'], 'application_id': '0002', 'external_course': '2',
    })
    assert result['recomputed_credits'] == 3
    assert result['available_credits'] == 3
    assert result['status'] == 'READY'
    assert [p['external_course'] for p in result['pairs'] if p['active']] == ['1']


def test_late_old_correction_does_not_restore_withdrawn_course(store, clean):
    run = approved_run(store, clean)
    withdraw(store, {
        'run_id': run['id'], 'application_id': '0001', 'external_course': '1',
    })
    stale = correct(store, {
        'run_id': run['id'], 'application_id': '0001',
        'external_course': '1', 'credits': 99, 'effective_at': '2000-01-01T00:00:00+00:00',
    })
    assert stale['available_credits'] == 0
    assert stale['last_correction']['applied'] is False
    assert stale['last_correction']['reason'] == 'STALE_CORRECTION'


def test_replacement_correction_restores_deduction(store, clean):
    run = approved_run(store, clean)
    withdraw(store, {
        'run_id': run['id'], 'application_id': '0001', 'external_course': '1',
    })
    insufficient = correct(store, {
        'application_id': '0001', 'external_course': '1',
        'new_external_course': '9', 'local_course': '2', 'credits': 1,
    })
    assert insufficient['available_credits'] == 0

    restored = correct(store, {
        'application_id': '0001', 'external_course': '1',
        'new_external_course': '9', 'local_course': '2', 'credits': 6,
    })
    assert restored['recomputed_credits'] == 10
    assert restored['available_credits'] == 10
    assert restored['deduction_valid'] is True
    assert restored['restored'] is True


def test_simultaneous_withdrawal_keeps_withdrawn_course_out():
    ledger = CreditLedger.issue({
        'application_id': 'SIM', 'minimum_credits': 6,
        'pairs': [
            {'external_course': '1', 'local_course': '1', 'credits': 6},
            {'external_course': '2', 'local_course': '2', 'credits': 4},
        ],
    })
    result = ledger.withdraw({
        'external_course': '1',
        'correction': {'external_course': '1', 'credits': 6},
        'simultaneous': True,
    })
    withdrawn = next(p for p in result['pairs'] if p['external_course'] == '1')
    assert withdrawn['active'] is False
    assert result['recomputed_credits'] == 4


def test_workbench_and_original_certificate_are_stable(client, store, clean):
    run = client.post('/api/runs', data={'sample': 'clean'}).json
    approved = client.patch(f"/api/runs/{run['id']}", json={
        'revision': 0, 'status': 'APPROVED', 'note': '凭证生成后不再改变',
    })
    assert approved.status_code == 200

    original = client.get(f"/api/certificates/{run['id']}")
    assert original.status_code == 200
    assert original.data == client.get(f"/api/runs/{run['id']}/evidence").data

    changed = client.post('/api/applications/0001/withdrawals', json={'external_course': '1'})
    assert changed.status_code == 200 and changed.json['available_credits'] == 0

    workbench = client.get('/api/workbench/credits')
    assert workbench.status_code == 200
    credits = workbench.json['credits']
    assert credits['0001'] == 0 and credits['0002'] == 3

    again = client.get(f"/api/certificates/{run['id']}")
    assert again.status_code == 200 and again.data == original.data


def test_direct_application_http_lifecycle(client):
    created = client.post('/api/applications', json={
        'application_id': 'A-1',
        'minimum_credits': 5,
        'pairs': [{'external_course': 'E1', 'local_course': 'L1', 'credits': 5}],
    })
    assert created.status_code == 200
    assert created.json['available_credits'] == 5

    withdrawn = client.post('/api/applications/A-1/withdrawals', json={'external_course': 'E1'})
    assert withdrawn.status_code == 200 and withdrawn.json['available_credits'] == 0

    missing = client.get('/api/applications/NOPE')
    assert missing.status_code == 404 and missing.json['code'] == 'APPLICATION_NOT_FOUND'
