"""Persisted demo offset; never changes OS clock or privacy TTL."""
from datetime import datetime, timedelta, timezone
from flask import current_app, has_app_context
from werkzeug.exceptions import BadRequest, Forbidden


def real_now():
    return datetime.now(timezone.utc)


def effective_now(db=None):
    if not has_app_context():
        return real_now()
    if db is None:
        from .db import get_db
        db = get_db()
    row = db.execute('SELECT offset_seconds FROM demo_clock WHERE id=1').fetchone()
    return real_now() + timedelta(seconds=row[0] if row else 0)


def iso_now(db=None):
    return effective_now(db).isoformat(timespec='microseconds')


def advance(db, actor, seconds):
    if not current_app.config['DEMO_MODE'] or actor['role'] != 'service':
        raise Forbidden('仅演示模式的企服可快进时间')
    try:
        seconds = int(seconds)
    except (ValueError, TypeError):
        raise BadRequest('快进秒数必须为整数')
    if not 1 <= seconds <= 86400:
        raise BadRequest('每次只允许向前快进 1–86400 秒；不能单独重置时钟')
    db.execute('BEGIN IMMEDIATE')
    try:
        db.execute('UPDATE demo_clock SET offset_seconds=offset_seconds+? WHERE id=1', (seconds,))
        db.execute('INSERT INTO clock_events(actor_id,seconds,real_recorded_at) VALUES(?,?,?)',
                   (actor['id'], seconds, real_now().isoformat()))
        db.commit()
    except Exception:
        db.rollback()
        raise


def evaluate(db):
    """Same timing rules for page evaluation and the opt-in Feishu worker."""
    stamp = iso_now(db)
    now = datetime.fromisoformat(stamp)
    db.execute('BEGIN IMMEDIATE')
    try:
        db.execute('UPDATE reminders SET resolved_at=? WHERE resolved_at IS NULL', (stamp,))
        flags = {}
        for row in db.execute('''SELECT t.*,a.pushed_at FROM tickets t
              LEFT JOIN assignments a ON a.id=t.current_assignment_id''').fetchall():
            phone = (row['status'] == 'pending_acceptance' and row['priority'] == '特急'
                     and row['pushed_at'] is not None
                     and (now - datetime.fromisoformat(row['pushed_at'])).total_seconds() >= 300)
            due = datetime.fromisoformat(row['due_at']) if row['due_at'] else None
            overdue = row['status'] != 'closed' and due is not None and now > due
            late_closed = row['status'] == 'closed' and due is not None and row['closed_at'] and datetime.fromisoformat(row['closed_at']) > due
            flags[row['id']] = dict(phone_followup=bool(phone), overdue=bool(overdue),
                historical_late_closed=bool(late_closed), deadline_unconfigured=due is None)
            for kind, active, scope in [('phone', phone, str(row['current_assignment_id'])),
                                         ('overdue', overdue, str(row['due_version']))]:
                if not active:
                    continue
                db.execute('''INSERT INTO reminders(ticket_id,kind,scope,first_detected_at)
                    VALUES(?,?,?,?) ON CONFLICT(ticket_id,kind,scope) DO UPDATE SET resolved_at=NULL''',
                    (row['id'], kind, scope, stamp))
                from .notifications import enqueue_reminder
                enqueue_reminder(db, row, kind, scope)
        db.commit()
        return flags
    except Exception:
        db.rollback()
        raise
