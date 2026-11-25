from app.business import DomainIssue
from app.intake.service import inspect
from app.modules.calculate import compute
from app.modules.grouping import case_groups
from app.modules.policy import policy
from app.modules.prepare import prepare
from app.modules.reconcile import balance


def case_ledger(payload, measure):
    sources, targets, edges, floor = payload
    edge_by_pair = {(str(source), str(target)): value for (source, target), value in edges.items()}
    pairs = []
    for external, local in measure.get('evidence', {}).get('matched', {}).items():
        credits, priority = edge_by_pair.get((str(external), str(local)), (0, 0))
        pairs.append({
            'external_course': str(external),
            'local_course': str(local),
            'credits': credits,
            'priority': priority,
        })
    return {
        'minimum_credits': floor,
        'external_courses': {str(key): {'credits': value} for key, value in sources.items()},
        'local_courses': {str(key): {'credits': value} for key, value in targets.items()},
        'pairs': pairs,
        'equivalences': [
            {'external_course': str(source), 'local_course': str(target),
             'credits': credits, 'priority': priority}
            for (source, target), (credits, priority) in edges.items()
        ],
    }


def evaluate(rows, stage):
    case = {'case':rows[0]['case'],'records':[r['record'] for r in rows], 'status':'READY'}
    try:
        case['prep'], payload = prepare(rows)
        if stage >= 3:
            case['measure'] = compute(payload)
            case['ledger'] = case_ledger(payload, case['measure'])
        if stage >= 4:
            case.update(policy(case['measure']))
    except DomainIssue as error:
        case['status'] = error.code
    return case


def run(data, stage=5):
    intake = inspect(data)
    output = {'intake': {k:v for k,v in intake.items() if k!='rows'}}
    if stage == 1:
        return output
    cases = [evaluate(rows, stage) for _, rows in case_groups(intake['rows'])]
    output['cases'] = cases
    if stage >= 5:
        output['balance'] = balance(intake,cases)
    return output
