from flask import Blueprint, Response, current_app, jsonify, request

from app import lifecycle
from app.business import DomainIssue
from app.evidence.render import artifact
from app.inputs import read_input
from app.modules.pipeline import run
from app.review.service import execute, review

api = Blueprint('api',__name__,url_prefix='/api')
STAGES = {'intake':1,'prepare':2,'calculate':3,'policy':4,'balance':5}


def payload():
    data = request.get_json(silent=True)
    if isinstance(data, list):
        return {**request.args.to_dict(flat=True), 'events': data}
    if isinstance(data, dict):
        return {**request.args.to_dict(flat=True), **data}
    if request.form:
        return {**request.args.to_dict(flat=True), **request.form.to_dict(flat=True)}
    return request.args.to_dict(flat=True)


@api.post('/stages/<stage>')
def preview(stage):
    if stage not in STAGES:
        raise DomainIssue('STAGE_UNKNOWN')
    return jsonify(run(read_input(),STAGES[stage]))


@api.route('/runs',methods=['GET','POST'])
def runs():
    store = current_app.extensions['runs']
    if request.method == 'GET':
        return jsonify(items=store.list())
    return jsonify(execute(store,read_input()))


@api.route('/runs/<run_id>', methods=['GET','PATCH'])
def batch(run_id):
    store = current_app.extensions['runs']
    if request.method == 'GET':
        return jsonify(store.load(run_id))
    return jsonify(review(store,run_id,request.get_json(silent=True)))


@api.get('/runs/<run_id>/evidence')
def export(run_id):
    store = current_app.extensions['runs']
    try:
        certificate = store.load_certificate(run_id)
        packet = certificate['artifact']
    except DomainIssue as error:
        if error.code != 'CERTIFICATE_NOT_FOUND':
            raise
        report = store.load(run_id)
        packet = artifact(report)
    return Response(packet,mimetype='application/json', headers={
        'Content-Disposition':f'attachment; filename="review-{run_id[:12]}.json"',
        'Cache-Control':'no-store',
    })


# ---------------------------------------------------------------------------
# Workbench credit positions
# ---------------------------------------------------------------------------

@api.get('/workbench')
@api.get('/workbench/credits')
@api.get('/credits')
def credit_workbench():
    store = current_app.extensions['runs']
    result = lifecycle.workbench(store, filters=payload())
    return jsonify(result)


@api.get('/credits/<identifier>')
@api.get('/applications')
@api.get('/applications/<identifier>')
@api.get('/applications/<identifier>/credits')
@api.get('/applications/<identifier>/credit')
def application_detail(identifier=None):
    store = current_app.extensions['runs']
    if request.path.endswith('/applications') and identifier is None:
        result = lifecycle.workbench(store, filters=payload())
    else:
        result = lifecycle.workbench(store, identifier=identifier, filters=payload())
    return jsonify(result)


@api.route('/applications/<identifier>', methods=['POST', 'PATCH', 'PUT'])
@api.route('/credits/<identifier>', methods=['POST', 'PATCH', 'PUT'])
def update_application(identifier):
    store = current_app.extensions['runs']
    data = payload()
    data.setdefault('application_id', identifier)
    action = str(data.get('action') or data.get('type') or data.get('event_type') or data.get('op') or data.get('status') or '').lower()
    if 'withdraw' in action or data.get('withdrawn') is True or data.get('withdraw') is True:
        return jsonify(lifecycle.withdraw(store, data))
    if 'settle' in action or 'event' in action or isinstance(data.get('events'), list):
        return jsonify(lifecycle.settle(store, data))
    if 'correct' in action or 'makeup' in action or 'supplement' in action or data.get('new_external_course') or data.get('replacement_course'):
        return jsonify(lifecycle.correct(store, data))
    if data.get('external_course') and data.get('credits') is not None and data.get('withdrawn') is not False:
        # Ambiguous flat PATCH: a negative action is explicit; credit-only
        # updates are corrections.
        if action in ('delete', 'remove', 'revoke'):
            return jsonify(lifecycle.withdraw(store, data))
        return jsonify(lifecycle.correct(store, data))
    raise DomainIssue('UNSUPPORTED_CREDIT_ACTION')


@api.post('/applications')
@api.post('/issues')
@api.post('/issuances')
@api.post('/awards')
@api.post('/positions')
def create_application():
    store = current_app.extensions['runs']
    return jsonify(lifecycle.create_application(store, payload()))


# ---------------------------------------------------------------------------
# Withdrawals and corrections
# ---------------------------------------------------------------------------

def _action(kind, identifier=None, run_id=None):
    store = current_app.extensions['runs']
    data = payload()
    if identifier is not None:
        data.setdefault('application_id', identifier)
    if run_id is not None:
        data.setdefault('run_id', run_id)
    if kind == 'withdraw':
        return jsonify(lifecycle.withdraw(store, data))
    if kind == 'correct':
        return jsonify(lifecycle.correct(store, data))
    return jsonify(lifecycle.settle(store, data))


@api.route('/applications/<identifier>/courses/<course>', methods=['DELETE', 'POST'])
@api.post('/applications/<identifier>/courses/<course>/withdraw')
def application_course_withdrawal(identifier, course):
    data = payload()
    data.update(application_id=identifier, external_course=course)
    return jsonify(lifecycle.withdraw(current_app.extensions['runs'], data))


@api.route('/pairs/<path:pair_id>', methods=['DELETE', 'POST'])
def pair_withdrawal(pair_id):
    data = payload()
    data.setdefault('pair_id', pair_id)
    if not (data.get('application_id') or data.get('run_id')):
        raise DomainIssue('APPLICATION_NOT_FOUND')
    return jsonify(lifecycle.withdraw(current_app.extensions['runs'], data))


@api.post('/withdraw')
@api.post('/withdrawals')
@api.post('/credit/withdraw')
@api.post('/credit/withdrawal')
@api.post('/credits/withdraw')
@api.post('/credits/withdrawal')
@api.post('/awards/withdraw')
@api.post('/recognitions/withdraw')
def withdrawal_collection():
    return _action('withdraw')


@api.post('/runs/<run_id>/withdraw')
@api.post('/runs/<run_id>/withdrawals')
def run_withdrawal(run_id):
    return _action('withdraw', run_id=run_id)


@api.post('/applications/<identifier>/withdraw')
@api.post('/applications/<identifier>/withdrawal')
@api.post('/applications/<identifier>/withdrawals')
@api.post('/applications/<identifier>/credits/withdraw')
@api.post('/applications/<identifier>/credit/withdraw')
def application_withdrawal(identifier):
    return _action('withdraw', identifier=identifier)


@api.post('/cases/<identifier>/withdraw')
@api.post('/cases/<identifier>/withdrawals')
def case_withdrawal(identifier):
    return _action('withdraw', identifier=identifier)


@api.post('/correct')
@api.post('/correction')
@api.post('/corrections')
@api.post('/credit/correct')
@api.post('/credit/correction')
@api.post('/credit/corrections')
@api.post('/credits/correct')
@api.post('/credits/correction')
@api.post('/credits/corrections')
@api.post('/makeup')
@api.post('/supplement/correct')
def correction_collection():
    return _action('correct')


@api.post('/runs/<run_id>/correct')
@api.post('/runs/<run_id>/correction')
@api.post('/runs/<run_id>/corrections')
def run_correction(run_id):
    return _action('correct', run_id=run_id)


@api.post('/applications/<identifier>/correct')
@api.post('/applications/<identifier>/correction')
@api.post('/applications/<identifier>/corrections')
@api.post('/applications/<identifier>/credits/correct')
@api.post('/applications/<identifier>/credits/corrections')
@api.post('/applications/<identifier>/makeup')
def application_correction(identifier):
    return _action('correct', identifier=identifier)


@api.post('/cases/<identifier>/correct')
@api.post('/cases/<identifier>/correction')
@api.post('/cases/<identifier>/corrections')
def case_correction(identifier):
    return _action('correct', identifier=identifier)


@api.post('/events')
@api.post('/settle')
@api.post('/lifecycle')
def event_settlement():
    return _action('settle')


@api.post('/applications/<identifier>/events')
@api.post('/applications/<identifier>/settle')
def application_settlement(identifier):
    return _action('settle', identifier=identifier)


# ---------------------------------------------------------------------------
# Immutable certificates / credentials / vouchers
# ---------------------------------------------------------------------------

def _certificate_response(issue_no, *, download=True):
    store = current_app.extensions['runs']
    cert = lifecycle.certificate_response(store, issue_no)
    safe_name = ''.join(ch if ch.isalnum() or ch in '._-' else '_' for ch in str(issue_no)[:64])
    headers = {'Cache-Control':'no-store'}
    if download:
        headers['Content-Disposition'] = f'attachment; filename="certificate-{safe_name}.json"'
    return Response(cert['artifact'], mimetype=cert.get('content_type') or 'application/json', headers=headers)


@api.get('/certificates')
@api.get('/certificate')
@api.get('/credentials')
@api.get('/vouchers')
def certificate_index():
    store = current_app.extensions['runs']
    return jsonify(items=store.list_certificates())


@api.get('/evidence/<path:issue_no>')
@api.get('/certificate/<path:issue_no>')
@api.get('/certificates/<path:issue_no>')
@api.get('/certificates/<path:issue_no>/download')
@api.get('/certificates/<path:issue_no>/evidence')
def certificate_download(issue_no):
    return _certificate_response(issue_no)


@api.get('/runs/<run_id>/certificate')
@api.get('/runs/<run_id>/certificates')
@api.get('/runs/<run_id>/credential')
def run_certificate_download(run_id):
    return _certificate_response(run_id)


@api.get('/credentials/<path:issue_no>')
@api.get('/credentials/<path:issue_no>/download')
@api.get('/vouchers/<path:issue_no>')
@api.get('/vouchers/<path:issue_no>/download')
@api.get('/issues/<path:issue_no>/certificate')
@api.get('/issues/<path:issue_no>/download')
@api.get('/issuances/<path:issue_no>/certificate')
@api.get('/awards/<path:issue_no>/certificate')
@api.get('/recognitions/<path:issue_no>/certificate')
def credential_download(issue_no):
    return _certificate_response(issue_no)
