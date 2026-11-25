import json
import sqlite3
from contextlib import closing, contextmanager
from pathlib import Path

from app.business import DomainIssue

SCHEMA = '''CREATE TABLE IF NOT EXISTS runs (
    id TEXT PRIMARY KEY, report TEXT NOT NULL, created TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'PENDING', revision INTEGER NOT NULL DEFAULT 0,
    note TEXT NOT NULL DEFAULT '', reviewed TEXT
);'''

LIFECYCLE_SCHEMA = '''
CREATE TABLE IF NOT EXISTS credit_positions (
    uid TEXT PRIMARY KEY,
    application_id TEXT NOT NULL,
    case_id TEXT,
    run_id TEXT,
    issue_no TEXT,
    revision INTEGER NOT NULL DEFAULT 0,
    state TEXT NOT NULL,
    updated TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_credit_positions_application ON credit_positions(application_id);
CREATE INDEX IF NOT EXISTS idx_credit_positions_case ON credit_positions(case_id);
CREATE INDEX IF NOT EXISTS idx_credit_positions_run ON credit_positions(run_id);
CREATE UNIQUE INDEX IF NOT EXISTS idx_credit_positions_issue ON credit_positions(issue_no) WHERE issue_no IS NOT NULL;

CREATE TABLE IF NOT EXISTS certificates (
    issue_no TEXT PRIMARY KEY,
    run_id TEXT,
    application_id TEXT,
    scope TEXT NOT NULL DEFAULT 'APPLICATION',
    content_type TEXT NOT NULL DEFAULT 'application/json',
    artifact BLOB NOT NULL,
    snapshot TEXT NOT NULL DEFAULT '{}',
    created TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_certificates_run ON certificates(run_id);
CREATE INDEX IF NOT EXISTS idx_certificates_application ON certificates(application_id);

CREATE TABLE IF NOT EXISTS credit_events (
    event_id TEXT PRIMARY KEY,
    application_uid TEXT NOT NULL,
    application_id TEXT NOT NULL,
    type TEXT NOT NULL,
    payload TEXT NOT NULL,
    effective_at TEXT,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_credit_events_application ON credit_events(application_uid);

CREATE TABLE IF NOT EXISTS run_inputs (
    run_id TEXT PRIMARY KEY,
    data BLOB NOT NULL
);
'''


class Store:
    def __init__(self, path):
        self.path = Path(path)
        self.memory_connection = None
        if str(path) == ':memory:':
            self.memory_connection = sqlite3.connect(':memory:', check_same_thread=False)
            self.memory_connection.row_factory = sqlite3.Row
            self.memory_connection.execute(SCHEMA)
            self.memory_connection.executescript(LIFECYCLE_SCHEMA)
            self.memory_connection.commit()

    @contextmanager
    def connect(self):
        if self.memory_connection is not None:
            # The benchmark uses file stores for concurrency tests; memory
            # support is mainly for lightweight unit tests.
            db = self.memory_connection
            db.row_factory = sqlite3.Row
            try:
                with db:
                    db.execute(SCHEMA)
                    db.executescript(LIFECYCLE_SCHEMA)
                    yield db
            except Exception:
                db.rollback()
                raise
            return

        self.path.parent.mkdir(parents=True, exist_ok=True)
        with closing(sqlite3.connect(self.path, timeout=15)) as db, db:
            db.row_factory = sqlite3.Row
            db.execute(SCHEMA)
            db.executescript(LIFECYCLE_SCHEMA)
            yield db

    def register(self, run_id, report, data=None):
        with self.connect() as db:
            db.execute('INSERT INTO runs(id,report,created) VALUES (?,?,?) ON CONFLICT(id) DO NOTHING',
                       (run_id,json.dumps(report,ensure_ascii=False),report['manifest']['created']))
            if data is not None:
                db.execute('INSERT INTO run_inputs(run_id,data) VALUES (?,?) ON CONFLICT(run_id) DO NOTHING',
                           (run_id, sqlite3.Binary(data)))
        return self.load(run_id)

    def save_input(self, run_id, data):
        with self.connect() as db:
            db.execute('INSERT INTO run_inputs(run_id,data) VALUES (?,?) ON CONFLICT(run_id) DO UPDATE SET data=excluded.data',
                       (run_id, sqlite3.Binary(data)))

    def load_input(self, run_id):
        with self.connect() as db:
            row = db.execute('SELECT data FROM run_inputs WHERE run_id=?',(run_id,)).fetchone()
        return bytes(row['data']) if row else None

    def load(self, run_id):
        with self.connect() as db:
            row = db.execute('SELECT * FROM runs WHERE id=?',(run_id,)).fetchone()
        if row is None:
            raise DomainIssue('RUN_NOT_FOUND')
        result = dict(row)
        result['report'] = json.loads(result['report'])
        result['issue_number'] = result['id']
        result['issue_no'] = result['id']
        result['certificate_no'] = result['id']
        result['certificates'] = self.certificates_for_run(run_id)
        result['issued_certificates'] = result['certificates']
        result['applications'] = self.list_positions(run_id)
        result['credit_positions'] = result['applications']
        result['available_credits'] = {
            item['application_id']: item['available_credits'] for item in result['applications']
        }
        return result

    def list(self):
        with self.connect() as db:
            return [dict(r) for r in db.execute('SELECT id,created,status,revision,note FROM runs ORDER BY created DESC,id LIMIT 50')]

    def revise(self, run_id, revision, status, note, now):
        with self.connect() as db:
            cursor = db.execute('UPDATE runs SET status=?,note=?,reviewed=?,revision=revision+1 WHERE id=? AND revision=?',
                                (status,note,now,run_id,revision))
            if cursor.rowcount != 1:
                raise DomainIssue('STALE_REVIEW')
        return self.load(run_id)

    # ----- credit positions -------------------------------------------------

    @staticmethod
    def _uid(position):
        run_id = position.get('run_id')
        app_id = position.get('application_id')
        if run_id and position.get('case_id'):
            return f'{run_id}:{position["case_id"]}'
        if run_id and app_id:
            return f'{run_id}:{app_id}'
        return app_id or position.get('issue_no')

    def upsert_position(self, position):
        uid = position.get('uid') or self._uid(position)
        position['uid'] = uid
        issue_no = position.get('issue_no') or position.get('issue_number')
        if issue_no:
            position['issue_no'] = issue_no
            position['issue_number'] = issue_no
        timestamp = position.get('updated_at')
        with self.connect() as db:
            db.execute('''
                INSERT INTO credit_positions
                  (uid,application_id,case_id,run_id,issue_no,revision,state,updated)
                VALUES (?,?,?,?,?,?,?,?)
                ON CONFLICT(uid) DO UPDATE SET
                  application_id=excluded.application_id,
                  case_id=excluded.case_id,
                  run_id=excluded.run_id,
                  issue_no=excluded.issue_no,
                  revision=excluded.revision,
                  state=excluded.state,
                  updated=excluded.updated
            ''', (
                uid, position.get('application_id'), position.get('case_id'),
                position.get('run_id'), issue_no, int(position.get('revision', 0)),
                json.dumps(position, ensure_ascii=False), timestamp,
            ))
        return self.load_position(uid)

    save_position = upsert_position
    save_application = upsert_position

    def _position_row_to_dict(self, row):
        position = json.loads(row['state'])
        position['_row'] = {key: row[key] for key in row.keys() if key != 'state'}
        return position

    def load_position(self, identifier):
        if identifier is None:
            raise DomainIssue('APPLICATION_NOT_FOUND')
        identifier = str(identifier)
        with self.connect() as db:
            row = db.execute('''
                SELECT * FROM credit_positions
                WHERE uid=? OR issue_no=? OR application_id=? OR case_id=?
                ORDER BY updated DESC LIMIT 1
            ''', (identifier, identifier, identifier, identifier)).fetchone()
        if row is None:
            # Some callers use a run id when the run has exactly one position.
            with self.connect() as db:
                rows = db.execute('SELECT * FROM credit_positions WHERE run_id=? ORDER BY updated DESC',
                                  (identifier,)).fetchall()
            if len(rows) == 1:
                return self._position_row_to_dict(rows[0])
            raise DomainIssue('APPLICATION_NOT_FOUND')
        return self._position_row_to_dict(row)

    get_position = load_position
    load_application = load_position
    get_application = load_position
    get_credit = load_position
    current_credits_position = load_position

    def list_positions(self, run_id=None):
        query = 'SELECT * FROM credit_positions'
        params: tuple = ()
        if run_id:
            query += ' WHERE run_id=?'
            params = (run_id,)
        query += ' ORDER BY updated DESC, uid LIMIT 200'
        with self.connect() as db:
            rows = db.execute(query, params).fetchall()
        return [self._position_row_to_dict(row) for row in rows]

    list_applications = list_positions
    workbench = list_positions

    # ----- event audit ------------------------------------------------------

    def save_event(self, event_id, application_uid, application_id, event_type, payload,
                   effective_at=None, created_at=None):
        with self.connect() as db:
            db.execute('''
                INSERT OR IGNORE INTO credit_events
                  (event_id,application_uid,application_id,type,payload,effective_at,created_at)
                VALUES (?,?,?,?,?,?,?)
            ''', (
                event_id, application_uid, application_id, event_type,
                json.dumps(payload, ensure_ascii=False), effective_at,
                created_at or payload.get('submitted_at') or payload.get('recorded_at'),
            ))

    def event_seen(self, event_id):
        if not event_id:
            return False
        with self.connect() as db:
            row = db.execute('SELECT 1 FROM credit_events WHERE event_id=?',(event_id,)).fetchone()
        return row is not None

    # ----- certificates -----------------------------------------------------

    def save_certificate(self, issue_no, artifact, *, run_id=None, application_id=None,
                         scope='APPLICATION', snapshot=None, created=None,
                         content_type='application/json'):
        if isinstance(artifact, str):
            artifact = artifact.encode()
        snapshot_text = snapshot if isinstance(snapshot, str) else json.dumps(snapshot or {}, ensure_ascii=False)
        with self.connect() as db:
            db.execute('''
                INSERT OR IGNORE INTO certificates
                  (issue_no,run_id,application_id,scope,content_type,artifact,snapshot,created)
                VALUES (?,?,?,?,?,?,?,?)
            ''', (issue_no, run_id, application_id, scope, content_type,
                  sqlite3.Binary(artifact), snapshot_text, created or ''))

    def load_certificate(self, issue_no):
        with self.connect() as db:
            row = db.execute('SELECT * FROM certificates WHERE issue_no=?',(str(issue_no),)).fetchone()
        if row is None:
            raise DomainIssue('CERTIFICATE_NOT_FOUND')
        result = dict(row)
        result['artifact'] = bytes(row['artifact'])
        try:
            result['snapshot'] = json.loads(result['snapshot'])
        except (TypeError, json.JSONDecodeError):
            result['snapshot'] = {}
        return result

    get_certificate = load_certificate

    def certificates_for_run(self, run_id):
        with self.connect() as db:
            rows = db.execute('''
                SELECT issue_no,run_id,application_id,scope,content_type,created
                FROM certificates WHERE run_id=? ORDER BY scope DESC, issue_no
            ''',(run_id,)).fetchall()
        items = [dict(r) for r in rows]
        for item in items:
            item['issue_number'] = item['issue_no']
            item['certificate_no'] = item['issue_no']
        return items

    def list_certificates(self):
        with self.connect() as db:
            rows = db.execute('''
                SELECT issue_no,run_id,application_id,scope,content_type,created
                FROM certificates ORDER BY created DESC, issue_no LIMIT 200
            ''').fetchall()
        items = [dict(r) for r in rows]
        for item in items:
            item['issue_number'] = item['issue_no']
        return items
