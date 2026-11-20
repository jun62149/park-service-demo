"""Transactional outbox and single-process App Bot dispatcher; real clock for retries."""
import functools
import json
import re
import threading
import time
import uuid
from datetime import datetime
from pathlib import Path

from flask import abort, current_app, g, has_app_context, jsonify

from .db import connect, get_db
from .feishu import AppBot, SendError

_locks = {}
_locks_guard = threading.Lock()


def enabled(config):
    return str(config.get('FEISHU_ENABLED', 'false')).lower() == 'true'


def lifecycle_lock(database):
    with _locks_guard:
        return _locks.setdefault(str(Path(database).resolve()), threading.RLock())


def serialize_reset(function):
    @functools.wraps(function)
    def wrapper(*args, **kwargs):
        # Serialize reset with actual sending, never hold a SQLite write transaction
        # while awaiting HTTP. An already submitted remote request cannot be recalled.
        with lifecycle_lock(current_app.config['DATABASE']):
            return function(*args, **kwargs)
    return wrapper


def safe_text(ticket, kind):
    from .workflow import STATES
    location = ticket['location'] if ticket['location'] in ('演示A座302', '演示公共走廊', '演示B座茶水间') else '演示位置（详见本地工单）'
    number = ticket['number'] if re.fullmatch(r'DEMO-[A-F0-9]{16}', ticket['number']) else '演示工单'
    category = next((label for word, label in [('空调', '空调故障'), ('灯', '照明故障'), ('水', '给排水故障'), ('电', '电气故障')]
                     if word in ticket['issue_description']), '设施报修')
    name = {2: '演示企服', 3: '演示工程甲', 4: '演示工程乙'}.get(ticket['assigned_engineer_id'], '演示责任人')
    title = {'assigned': '工单分派', 'completed': '完工待企服验收', 'returned': '退回重新处理',
             'phone': '特急待响应，请人工电话跟进', 'overdue': '工单超过人工截止时间'}[kind]
    return ('【演示数据 · ' + title + '】\n编号：' + number + '\n位置：' + location + '\n摘要：演示' + category +
            '\n状态：' + STATES[ticket['status']] + '\n优先级：' + (ticket['priority'] or '待确认') + '\n指定责任人：' + name)


def enqueue_event(db, event_id, ticket, action):
    kind = {'assign': 'assigned', 'reassign': 'assigned', 'submit_completion': 'completed', 'reject': 'returned'}.get(action)
    if not kind:
        return
    config = current_app.config if has_app_context() else {}
    target = ticket['assigned_engineer_id']
    if kind == 'completed':
        assignment = db.execute('SELECT assigned_by FROM assignments WHERE id=? AND ticket_id=?',
                                (ticket['current_assignment_id'], ticket['id'])).fetchone()
        if not assignment:
            return
        target = assignment['assigned_by']
    generation = db.execute('SELECT generation FROM demo_meta WHERE id=1').fetchone()[0]
    mapping = db.execute('SELECT enabled FROM feishu_bindings WHERE local_user_id=?', (target,)).fetchone()
    state, code = ('pending', None) if enabled(config) and mapping and mapping['enabled'] else (
        ('disabled', 'integration_disabled') if not enabled(config) else ('unconfigured', 'mapping_missing'))
    key = uuid.uuid5(uuid.NAMESPACE_URL, generation + ':event:' + str(event_id) + ':' + kind).hex
    db.execute('''INSERT OR IGNORE INTO notification_outbox
        (id,generation,event_id,ticket_id,kind,target_user_id,payload_text,status,attempts,next_attempt_at,error_code,created_at)
        VALUES(?,?,?,?,?,?,?,?,0,?,?,?)''',
        (key, generation, event_id, ticket['id'], kind, target, safe_text(ticket, kind), state, time.time(), code, time.time()))


def reminder_target(db, ticket, kind):
    assignment = db.execute('SELECT * FROM assignments WHERE id=?', (ticket['current_assignment_id'],)).fetchone()
    if kind == 'phone' or ticket['status'] == 'pending_service_review':
        return assignment['assigned_by'] if assignment else 2
    return ticket['assigned_engineer_id'] or 2


def reminder_key(generation, reminder_id, kind, target, assignment):
    scope = ':'.join(map(str, (reminder_id, kind, target, assignment)))
    return uuid.uuid5(uuid.NAMESPACE_URL, generation + ':reminder:' + scope).hex


def enqueue_reminder(db, ticket, kind, scope):
    config = current_app.config if has_app_context() else {}
    if not enabled(config):
        return # Preserve the original page-driven in-app reminder behavior when disabled.
    reminder = db.execute('SELECT id FROM reminders WHERE ticket_id=? AND kind=? AND scope=?',
                          (ticket['id'], kind, scope)).fetchone()
    generation = db.execute('SELECT generation FROM demo_meta WHERE id=1').fetchone()[0]
    target = reminder_target(db, ticket, kind)
    binding = db.execute('SELECT enabled FROM feishu_bindings WHERE local_user_id=?', (target,)).fetchone()
    state, code = ('pending', None) if binding and binding['enabled'] else ('unconfigured', 'mapping_missing')
    db.execute('''INSERT INTO notification_outbox
        (id,generation,event_id,ticket_id,kind,target_user_id,payload_text,status,attempts,next_attempt_at,error_code,created_at)
        VALUES(?,?,NULL,?,?,?,?,?,0,?,?,?) ON CONFLICT(id) DO UPDATE SET
        status='pending',error_code=NULL,next_attempt_at=excluded.next_attempt_at
        WHERE notification_outbox.status='cancelled' AND notification_outbox.error_code='event_obsolete'
        AND notification_outbox.attempts=0 AND excluded.status='pending' ''',
        (reminder_key(generation, reminder['id'], kind, target, ticket['current_assignment_id']), generation, ticket['id'], kind, target,
         safe_text(ticket, kind), state, time.time(), code, time.time()))


def relevant(db, row, ticket):
    if not ticket:
        return False
    if row['kind'] in ('phone', 'overdue'):
        from .clock import effective_now
        kind = row['kind']
        scope = str(ticket['current_assignment_id']) if kind == 'phone' else str(ticket['due_version'])
        reminder = db.execute('SELECT * FROM reminders WHERE ticket_id=? AND kind=? AND scope=?',
                              (ticket['id'], kind, scope)).fetchone()
        if (not reminder or reminder['resolved_at'] is not None or
                reminder_key(row['generation'], reminder['id'], kind, row['target_user_id'], ticket['current_assignment_id']) != row['id'] or
                reminder_target(db, ticket, kind) != row['target_user_id']):
            return False
        stamp = effective_now(db)
        if kind == 'phone':
            assignment = db.execute('SELECT pushed_at FROM assignments WHERE id=?', (ticket['current_assignment_id'],)).fetchone()
            return (ticket['status'] == 'pending_acceptance' and ticket['priority'] == '特急' and
                    assignment and assignment['pushed_at'] is not None and
                    (stamp - datetime.fromisoformat(assignment['pushed_at'])).total_seconds() >= 300)
        return (ticket['status'] != 'closed' and ticket['due_at'] is not None and
                stamp > datetime.fromisoformat(ticket['due_at']))
    event = db.execute('SELECT * FROM ticket_events WHERE id=? AND ticket_id=?',
                       (row['event_id'], ticket['id'])).fetchone()
    if not event or event['assignment_id'] != ticket['current_assignment_id']:
        return False
    if row['kind'] == 'assigned':
        return ticket['assigned_engineer_id'] == row['target_user_id'] and ticket['status'] == 'pending_acceptance'
    if row['kind'] == 'completed':
        assignment = db.execute('SELECT assigned_by FROM assignments WHERE id=?', (ticket['current_assignment_id'],)).fetchone()
        # A previous review round must not become valid again after reject/rework.
        return (assignment and assignment['assigned_by'] == row['target_user_id'] and
                ticket['status'] == 'pending_service_review' and ticket['version'] == event['ticket_version'])
    if row['kind'] == 'returned':
        return (ticket['assigned_engineer_id'] == row['target_user_id'] and ticket['status'] == 'returned' and
                ticket['version'] == event['ticket_version'])
    return False


def sync_bindings(db, path):
    """Startup/operator only. No web endpoint accepts recipients or configuration."""
    try:
        source = Path(path).resolve()
        if not source.is_relative_to(Path(current_app.config['DATABASE']).resolve().parent):
            raise ValueError()
        values = json.loads(source.read_text(encoding='utf-8-sig'))
        if not isinstance(values, dict):
            raise ValueError()
        prepared = []
        for uid, item in values.items():
            user = db.execute("SELECT id FROM users WHERE id=? AND active=1 AND role IN ('engineer','service')", (int(uid),)).fetchone()
            if not user or not isinstance(item, dict) or type(item.get('enabled')) is not bool or not re.fullmatch(r'ou_[A-Za-z0-9_-]{8,100}', item.get('open_id', '')):
                raise ValueError()
            prepared.append((int(uid), item['open_id'], int(item['enabled'])))
    except Exception:
        raise SendError('invalid_mapping_config') from None
    db.execute('BEGIN IMMEDIATE')
    try:
        db.execute('UPDATE feishu_bindings SET enabled=0')
        for uid, oid, active in prepared:
            db.execute('''INSERT INTO feishu_bindings(local_user_id,open_id,enabled,verification_status)
                VALUES(?,?,?,'unverified') ON CONFLICT(local_user_id) DO UPDATE SET
                verification_status=CASE WHEN feishu_bindings.open_id=excluded.open_id THEN feishu_bindings.verification_status ELSE 'unverified' END,
                last_verified_at=CASE WHEN feishu_bindings.open_id=excluded.open_id THEN feishu_bindings.last_verified_at ELSE NULL END,
                open_id=excluded.open_id,enabled=excluded.enabled''', (uid, oid, active))
        db.commit()
    except Exception:
        db.rollback(); raise


class Dispatcher:
    def __init__(self, app, client=None):
        self.app, self.client = app, client or AppBot(app.config)
        self.stop_event = threading.Event()
        self.thread = None
        self.lease = None

    def dispatch_one(self, notification_id=None):
        with self.app.app_context():
            return self._dispatch_one(notification_id)

    def _dispatch_one(self, notification_id=None):
        db = connect(self.app.config['DATABASE'])
        try:
            with lifecycle_lock(self.app.config['DATABASE']):
                db.execute('BEGIN IMMEDIATE')
                try:
                    generation = db.execute('SELECT generation FROM demo_meta WHERE id=1').fetchone()[0]
                    sql = "SELECT * FROM notification_outbox WHERE generation=? AND status='pending' AND next_attempt_at<=?"
                    args = (generation, time.time())
                    if notification_id:
                        sql += ' AND id=?'; args += (notification_id,)
                    row = db.execute(sql + ' ORDER BY created_at,id LIMIT 1', args).fetchone()
                    if not row:
                        db.commit(); return False
                    row = dict(row)
                    ticket = db.execute('SELECT * FROM tickets WHERE id=?', (row['ticket_id'],)).fetchone()
                    binding = db.execute('SELECT * FROM feishu_bindings WHERE local_user_id=? AND enabled=1', (row['target_user_id'],)).fetchone()
                    if not enabled(self.app.config):
                        terminal, code = 'disabled', 'integration_disabled'
                    elif not binding:
                        terminal, code = 'unconfigured', 'mapping_missing'
                    elif not relevant(db, row, ticket):
                        terminal, code = 'cancelled', 'event_obsolete'
                    else:
                        terminal, code = 'sending', None
                    db.execute('UPDATE notification_outbox SET status=?,error_code=?,attempts=attempts+? WHERE id=?', (terminal, code, int(terminal == 'sending'), row['id']))
                    db.commit()
                    if terminal != 'sending':
                        return True
                except Exception:
                    db.rollback(); raise
                result, error, retry = 'sent', None, False
                try:
                    assert not db.in_transaction
                    self.client.send(binding['open_id'], row['payload_text'], row['id'])
                except SendError as failure:
                    result = 'uncertain' if failure.uncertain else 'failed'
                    error, retry = failure.code, failure.retry
                except Exception:
                    result, error = 'uncertain', 'dispatcher_error'
                db.execute('BEGIN IMMEDIATE')
                try:
                    # Completion gate is independently necessary even with the single-process
                    # lifecycle lock: never resurrect a deleted row or a previous generation.
                    current = db.execute('SELECT generation FROM demo_meta WHERE id=1').fetchone()[0]
                    if current == generation:
                        attempts = row['attempts'] + 1
                        if retry and attempts < 3:
                            result = 'pending'
                        db.execute('''UPDATE notification_outbox SET status=?,error_code=?,next_attempt_at=?,sent_at=?
                            WHERE id=? AND generation=? AND status='sending' ''',
                            (result, error, time.time() + (5 if attempts == 1 else 30), time.time() if result == 'sent' else None, row['id'], generation))
                        if result == 'sent':
                            db.execute("UPDATE feishu_bindings SET verification_status='api_accepted',last_verified_at=? WHERE local_user_id=? AND open_id=?", (time.time(), row['target_user_id'], binding['open_id']))
                    db.commit()
                except Exception:
                    db.rollback(); raise
                return True
        finally:
            db.close()

    def start(self):
        if not enabled(self.app.config):
            return
        root = Path(self.app.config['DATABASE']).resolve().parent
        self.lease = (root / 'feishu_worker.lock').open('a+b')
        try:
            import msvcrt
            self.lease.seek(0); self.lease.write(b'0'); self.lease.flush(); self.lease.seek(0)
            msvcrt.locking(self.lease.fileno(), msvcrt.LK_NBLCK, 1)
        except Exception:
            self.lease.close(); self.lease = None
            raise SendError('worker_lock_unavailable') from None
        try:
            with self.app.app_context():
                db = get_db()
                if Path(self.app.config['FEISHU_BINDINGS_FILE']).is_file():
                    sync_bindings(db, self.app.config['FEISHU_BINDINGS_FILE'])
                # A crashed sending attempt may already have reached Feishu. Do not auto-resend.
                db.execute("UPDATE notification_outbox SET status='uncertain',error_code='process_interrupted' WHERE status='sending'")
            self.thread = threading.Thread(target=self._run, name='feishu-outbox', daemon=True)
            self.thread.start()
        except SendError as error:
            if error.code == 'invalid_mapping_config':
                self.app.config['FEISHU_ENABLED'] = 'false'
                self.app.logger.error('飞书通知未启用，代码=invalid_mapping_config')
                self.close()
                return
            self.close(); raise
        except Exception:
            self.close(); raise

    def _run(self):
        while not self.stop_event.is_set():
            try:
                self.evaluate_reminders()
                self.dispatch_one()
            except Exception:
                self.app.logger.error('飞书通知处理失败，代码=worker_error')
            self.stop_event.wait(1)

    def evaluate_reminders(self):
        if enabled(self.app.config):
            from .clock import evaluate
            # Reset cannot change the clock generation between reading the timestamp
            # and the reminder transaction. This lock never surrounds DB+network together.
            with lifecycle_lock(self.app.config['DATABASE']), self.app.app_context():
                return evaluate(get_db())

    def close(self):
        self.stop_event.set()
        if self.thread:
            self.thread.join(timeout=12)
            if self.thread.is_alive():
                return  # Keep the lease until process exit if a transport is still in flight.
        if self.lease:
            self.lease.close(); self.lease = None


def init_app(app):
    app.extensions['feishu_dispatcher'] = Dispatcher(app)
    @app.get('/notifications')
    def status():
        if g.actor['role'] not in ('service', 'engineer'):
            abort(403, '当前身份不能查看通知状态')
        sql = '''SELECT id,event_id,ticket_id,kind,target_user_id,status,attempts,error_code,sent_at
                 FROM notification_outbox'''
        args = ()
        if g.actor['role'] == 'engineer':
            sql += ' WHERE target_user_id=?'; args = (g.actor['id'],)
        rows = [dict(r) for r in get_db().execute(sql + ' ORDER BY created_at DESC LIMIT 100', args)]
        return jsonify(notifications=rows, sent_means='飞书API已接受，不代表客户端确认收到',
                       enabled=enabled(app.config))
