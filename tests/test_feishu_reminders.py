"""Feishu reminder routing/dedup; deterministic demo clock, local recording client."""
import re
import threading
from datetime import datetime, timedelta, timezone

import pytest

from park_service import clock
from park_service.feishu import SendError
from park_service.notifications import Dispatcher


@pytest.fixture
def timed_notify(app, browser, db, monkeypatch):
    base = [datetime(2027, 1, 1, tzinfo=timezone.utc)]
    monkeypatch.setattr(clock, 'real_now', lambda: base[0])
    app.config['FEISHU_ENABLED'] = 'true'
    db.executemany('INSERT INTO feishu_bindings(local_user_id,open_id,enabled) VALUES(?,?,1)',
                   [(2, 'ou_FAKE_SERVICE_REMINDER'), (3, 'ou_FAKE_ENGINEER_REMINDER')])
    return app, browser, db, base


class Recorder:
    def __init__(self): self.calls = []; self.called = threading.Event()
    def send(self, recipient, text, key):
        self.calls.append((recipient, text, key)); self.called.set()


def jobs(db, kind):
    return [dict(r) for r in db.execute('SELECT * FROM notification_outbox WHERE kind=? ORDER BY created_at,id', (kind,))]


def urgent(b):
    b.role(2)
    assert b.action('assign', {'engineer_id': 3, 'priority': '特急', 'priority_confirmed': 'yes'}).status_code == 200


@pytest.mark.parametrize('seconds,expected', [(299, 0), (300, 1), (301, 1)])
def test_phone_boundary_target_and_dedup(timed_notify, seconds, expected):
    app, b, db, base = timed_notify
    urgent(b); base[0] += timedelta(seconds=seconds)
    recorder = Recorder(); dispatcher = Dispatcher(app, recorder)
    dispatcher.evaluate_reminders(); dispatcher.evaluate_reminders()
    rows = jobs(db, 'phone')
    assert len(rows) == expected
    if rows:
        assert rows[0]['target_user_id'] == 2
        dispatcher.dispatch_one(rows[0]['id'])
        assert recorder.calls[0][0] == 'ou_FAKE_SERVICE_REMINDER'
        assert '人工电话跟进' in recorder.calls[0][1]
        dispatcher.evaluate_reminders()
        assert not dispatcher.dispatch_one(rows[0]['id']) and len(recorder.calls) == 1


def test_accept_cancels_pending_phone_and_preserves_history(timed_notify):
    app, b, db, base = timed_notify
    urgent(b); base[0] += timedelta(seconds=300)
    recorder = Recorder(); dispatcher = Dispatcher(app, recorder)
    dispatcher.evaluate_reminders(); key = jobs(db, 'phone')[0]['id']
    b.role(3); assert b.action('accept').status_code == 200
    dispatcher.dispatch_one(key); dispatcher.evaluate_reminders()
    assert not recorder.calls and jobs(db, 'phone')[0]['status'] == 'cancelled'
    assert db.execute("SELECT resolved_at FROM reminders WHERE kind='phone'").fetchone()[0]


def test_phone_reassignment_gets_new_scope(timed_notify):
    app, b, db, base = timed_notify
    urgent(b); base[0] += timedelta(seconds=300)
    recorder = Recorder(); dispatcher = Dispatcher(app, recorder)
    dispatcher.evaluate_reminders(); old = jobs(db, 'phone')[0]['id']
    assert b.action('reassign', {'engineer_id': 4, 'priority': '特急', 'priority_confirmed': 'yes', 'reason': '演示改派'}).status_code == 200
    base[0] += timedelta(seconds=299); dispatcher.evaluate_reminders()
    assert len(jobs(db, 'phone')) == 1
    dispatcher.dispatch_one(old); assert not recorder.calls
    base[0] += timedelta(seconds=1); dispatcher.evaluate_reminders()
    rows = jobs(db, 'phone'); assert len(rows) == 2
    dispatcher.dispatch_one(next(r['id'] for r in rows if r['id'] != old))
    assert len(recorder.calls) == 1 and recorder.calls[0][0] == 'ou_FAKE_SERVICE_REMINDER'


def test_priority_reactivation_only_requeues_never_attempted_notice(timed_notify):
    app, b, db, base = timed_notify
    urgent(b); base[0] += timedelta(seconds=300)
    recorder = Recorder(); dispatcher = Dispatcher(app, recorder)
    dispatcher.evaluate_reminders(); key = jobs(db, 'phone')[0]['id']
    assert b.action('update_assignment', {'priority': '普通', 'priority_confirmed': 'yes', 'reason': '演示降级'}).status_code == 200
    dispatcher.dispatch_one(key); assert not recorder.calls
    assert b.action('update_assignment', {'priority': '特急', 'priority_confirmed': 'yes', 'reason': '演示升级'}).status_code == 200
    dispatcher.evaluate_reminders(); dispatcher.dispatch_one(key)
    assert len(recorder.calls) == 1
    dispatcher.evaluate_reminders(); assert not dispatcher.dispatch_one(key)


@pytest.mark.parametrize('seconds,expected', [(-1, 0), (0, 0), (1, 1)])
def test_due_boundary_and_no_inferred_sla(timed_notify, seconds, expected):
    app, b, db, base = timed_notify
    b.assign()
    dispatcher = Dispatcher(app, Recorder())
    dispatcher.evaluate_reminders(); assert not jobs(db, 'overdue')
    db.execute('UPDATE tickets SET due_at=? WHERE id=1', ((base[0] - timedelta(seconds=seconds)).isoformat(),))
    dispatcher.evaluate_reminders()
    rows = jobs(db, 'overdue'); assert len(rows) == expected
    if rows: assert rows[0]['target_user_id'] == 3


def test_due_version_change_and_clear_cancel_pending(timed_notify):
    app, b, db, base = timed_notify
    b.assign(); dispatcher = Dispatcher(app, Recorder())
    ids = []
    for seconds in (1, 2):
        assert b.action('update_assignment', {'priority': '普通', 'priority_confirmed': 'yes',
            'due_at': (base[0] - timedelta(seconds=seconds)).isoformat(), 'reason': '演示截止变更'}).status_code == 200
        dispatcher.evaluate_reminders()
        ids.append(next(r['id'] for r in jobs(db, 'overdue') if r['id'] not in ids))
    dispatcher.dispatch_one(ids[0])
    assert not dispatcher.client.calls
    assert b.action('update_assignment', {'priority': '普通', 'priority_confirmed': 'yes', 'due_at': '', 'reason': '演示清空'}).status_code == 200
    dispatcher.dispatch_one(ids[1]); dispatcher.evaluate_reminders()
    assert not dispatcher.client.calls and all(r['status'] == 'cancelled' for r in jobs(db, 'overdue'))


def test_overdue_responsibility_moves_to_review_service(timed_notify):
    app, b, db, base = timed_notify
    b.processing()
    db.execute('UPDATE tickets SET due_at=? WHERE id=1', ((base[0] - timedelta(seconds=1)).isoformat(),))
    recorder = Recorder(); dispatcher = Dispatcher(app, recorder)
    dispatcher.evaluate_reminders(); engineer = jobs(db, 'overdue')[0]
    assert b.complete().status_code == 200
    dispatcher.evaluate_reminders()
    service = next(r for r in jobs(db, 'overdue') if r['target_user_id'] == 2)
    assert service['id'] != engineer['id']
    dispatcher.dispatch_one(engineer['id']); assert not recorder.calls
    dispatcher.dispatch_one(service['id'])
    assert recorder.calls[0][0] == 'ou_FAKE_SERVICE_REMINDER'
    assert db.execute("SELECT count(*) FROM reminders WHERE kind='overdue'").fetchone()[0] == 1


def test_closed_ticket_cancels_pending_review_and_overdue(timed_notify):
    app, b, db, base = timed_notify
    b.review()
    db.execute('UPDATE tickets SET due_at=? WHERE id=1', ((base[0] - timedelta(seconds=1)).isoformat(),))
    recorder = Recorder(); dispatcher = Dispatcher(app, recorder)
    dispatcher.evaluate_reminders()
    keys = [r['id'] for kind in ('completed', 'overdue') for r in jobs(db, kind)]
    b.role(2); assert b.action('close', {'review_confirmed': 'yes'}).status_code == 200
    for key in keys: dispatcher.dispatch_one(key)
    assert not recorder.calls


def test_return_notice_targets_engineer_and_sends_once(timed_notify):
    app, b, db, base = timed_notify
    b.review(); b.role(2)
    assert b.action('reject', {'reason': '演示补充凭证'}).status_code == 200
    recorder = Recorder(); dispatcher = Dispatcher(app, recorder)
    job = jobs(db, 'returned')[0]
    assert job['target_user_id'] == 3 and '补充凭证' not in job['payload_text']
    dispatcher.dispatch_one(job['id'])
    assert recorder.calls[0][0] == 'ou_FAKE_ENGINEER_REMINDER' and '退回重新处理' in recorder.calls[0][1]
    assert not dispatcher.dispatch_one(job['id'])


def test_pending_return_notice_is_cancelled_after_resume(timed_notify):
    app, b, db, base = timed_notify
    b.review(); b.role(2)
    assert b.action('reject', {'reason': '演示复核'}).status_code == 200
    job = jobs(db, 'returned')[0]
    b.role(3); assert b.action('resume').status_code == 200
    recorder = Recorder(); Dispatcher(app, recorder).dispatch_one(job['id'])
    assert not recorder.calls and jobs(db, 'returned')[0]['status'] == 'cancelled'


def test_reset_clears_reminder_jobs_and_background_does_not_restore(timed_notify):
    app, b, db, base = timed_notify
    urgent(b); base[0] += timedelta(seconds=300)
    dispatcher = Dispatcher(app, Recorder()); dispatcher.evaluate_reminders()
    assert jobs(db, 'phone')
    html = b.client.get('/demo/reset-data').get_data(as_text=True)
    token = re.search(r'name="reset_token" value="([^"]+)"', html).group(1)
    assert b.post('/demo/reset-data', {'reset_token': token, 'confirmation': '重置演示数据'}).status_code == 200
    dispatcher.evaluate_reminders(); assert not dispatcher.dispatch_one()
    assert db.execute('SELECT count(*) FROM notification_outbox').fetchone()[0] == 0
    assert db.execute('SELECT offset_seconds FROM demo_clock').fetchone()[0] == 0


def test_uncertain_reminder_not_reopened_by_repeated_evaluation(timed_notify):
    app, b, db, base = timed_notify
    urgent(b); base[0] += timedelta(seconds=300)
    class Uncertain:
        def send(self, *args): raise SendError('send_timeout', uncertain=True)
    dispatcher = Dispatcher(app, Uncertain()); dispatcher.evaluate_reminders()
    key = jobs(db, 'phone')[0]['id']; dispatcher.dispatch_one(key)
    dispatcher.evaluate_reminders(); dispatcher.evaluate_reminders()
    assert not dispatcher.dispatch_one(key) and jobs(db, 'phone')[0]['status'] == 'uncertain'


def test_background_evaluates_without_page_request(timed_notify):
    app, b, db, base = timed_notify
    urgent(b); base[0] += timedelta(seconds=301)
    recorder = Recorder(); dispatcher = Dispatcher(app, recorder)
    try:
        dispatcher.start()
        assert recorder.called.wait(5)
        assert len(jobs(db, 'phone')) == 1
    finally: dispatcher.close()


def test_dispatch_thread_uses_demo_offset_with_real_clock_unchanged(timed_notify):
    app, b, db, base = timed_notify
    urgent(b)
    db.execute('UPDATE tickets SET due_at=? WHERE id=1', ((base[0] + timedelta(seconds=60)).isoformat(),))
    real = base[0]
    assert b.post('/demo/advance', {'seconds': 300}).status_code == 200
    assert base[0] == real
    recorder = Recorder(); dispatcher = Dispatcher(app, recorder)
    dispatcher.evaluate_reminders()
    for kind in ('phone', 'overdue'):
        key = jobs(db, kind)[0]['id']
        assert dispatcher.dispatch_one(key)
        assert jobs(db, kind)[0]['status'] == 'sent'
    assert len(recorder.calls) == 2
