import json


def artifact(run):
    cases = []
    for case in run['report']['cases']:
        # Ledger is the post-issuance recomputation adapter; the frozen review
        # evidence keeps the original stage contract.
        cases.append({key: value for key, value in case.items() if key != 'ledger'})
    packet = {
        'schema': 'review-evidence/1',
        'manifest': run['report']['manifest'],
        'balance': run['report']['balance'],
        'cases': cases,
        'intake': run['report']['intake'],
        'review': {key:run[key] for key in ('status','revision','note','reviewed')},
    }
    return (json.dumps(packet, ensure_ascii=False,sort_keys=True,indent=2)+'\n').encode()
