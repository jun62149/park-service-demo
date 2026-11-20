"""Stage 5: all identities/tokens are conspicuous fakes; HTTP runs on loopback."""
import json
import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor, TimeoutError
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest
import requests

from park_service import feishu
from park_service.db import connect
from park_service.feishu import AppBot, SendError
from park_service.notifications import Dispatcher, sync_bindings
from .conftest import Browser, image_bytes

FAKE_ID = 'ou_FAKE_ENGINEER_TEST_ONLY'


@pytest.fixture
def configured(app, db):
    app.config.update(FEISHU_ENABLED='true', FEISHU_APP_ID='FAKE_APP', FEISHU_APP_SECRET='FAKE_SECRET')
    db.execute("INSERT INTO feishu_bindings(local_user_id,open_id,enabled) VALUES(?,?,1)", (3, FAKE_ID))
    return app


@pytest.fixture
def stub(monkeypatch):
    calls, responses = [], []
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args): pass
        def do_POST(self):
            body = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
            calls.append({'path': self.path, 'body': body, 'auth': self.headers.get('Authorization')})
            if responses:
                status, payload = responses.pop(0)
            elif self.path == '/token':
                status, payload = 200, {'code': 0, 'tenant_access_token': 'FAKE_TOKEN', 'expire': 7200}
            else:
                status, payload = 200, {'code': 0, 'data': {'message_id': 'FAKE_MESSAGE'}}
            self.send_response(status); self.send_header('Content-Type', 'application/json'); self.end_headers()
            self.wfile.write(json.dumps(payload).encode())
    server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True); thread.start()
    monkeypatch.setattr(feishu, 'TOKEN_URL', f'http://127.0.0.1:{server.server_port}/token')
    monkeypatch.setattr(feishu, 'MESSAGE_URL', f'http://127.0.0.1:{server.server_port}/messages')
    yield calls, responses
    server.shutdown(); server.server_close(); thread.join()


def row(db):
    return dict(db.execute('SELECT * FROM notification_outbox ORDER BY created_at LIMIT 1').fetchone())


def reset(b):
    b.role(2)
    html = b.client.get('/demo/reset-data').get_data(as_text=True)
    nonce = re.search(r'name="reset_token" value="([^"]+)"', html).group(1)
    return b.post('/demo/reset-data', {'reset_token': nonce, 'confirmation': '重置演示数据'})


def test_assignment_real_http_stub_committed_and_private(configured, browser, db, stub):
    calls, _ = stub
    browser.assign()
    assert row(db)['status'] == 'pending' and not calls
    outbox_id = row(db)['id']
    assert Dispatcher(configured).dispatch_one()
    assert row(db)['status'] == 'sent' and row(db)['attempts'] == 1
    assert [c['path'] for c in calls] == ['/token', '/messages?receive_id_type=open_id']
    assert calls[0]['body'] == {'app_id': 'FAKE_APP', 'app_secret': 'FAKE_SECRET'}
    assert calls[1]['auth'] == 'Bearer FAKE_TOKEN'
    assert calls[1]['body']['receive_id'] == FAKE_ID and calls[1]['body']['uuid'] == outbox_id
    text = json.loads(calls[1]['body']['content'])['text']
    assert '演示数据' in text and '待接单' in text and 'DEMO-前台分机' not in text
    assert db.execute('SELECT verification_status FROM feishu_bindings').fetchone()[0] == 'api_accepted'
    status = browser.get('/notifications')
    assert status.status_code == 200
    assert FAKE_ID not in status.get_data(as_text=True)
    assert 'FAKE_SECRET' not in status.get_data(as_text=True) and 'FAKE_TOKEN' not in status.get_data(as_text=True)
    assert browser.action('assign', version=1).status_code == 409
    assert db.execute('SELECT count(*) FROM notification_outbox').fetchone()[0] == 1


@pytest.mark.parametrize('mode,state', [('disabled', 'disabled'), ('missing_mapping', 'unconfigured')])
def test_closed_integration_and_missing_mapping_do_not_block(app, browser, db, mode, state):
    app.config['FEISHU_ENABLED'] = mode != 'disabled'
    browser.assign()
    assert row(db)['status'] == state and browser.detail()['ticket']['status'] == 'pending_acceptance'
    class Never:
        def send(self, *args): pytest.fail('Must not send')
    assert not Dispatcher(app, Never()).dispatch_one()


def test_concurrent_dispatch_is_single_send(configured, browser, db):
    browser.assign(); calls = []
    class Local:
        def send(self, *args):
            check = connect(configured.config['DATABASE'])
            try:
                assert check.execute('SELECT status FROM notification_outbox').fetchone()[0] == 'sending'
                assert check.execute('SELECT status FROM tickets WHERE id=1').fetchone()[0] == 'pending_acceptance'
            finally: check.close()
            calls.append('local')
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _: Dispatcher(configured, Local()).dispatch_one(), range(2)))
    assert sorted(results) == [False, True] and len(calls) == 1


@pytest.mark.parametrize('status,code,expected', [
    (401, 0, 'authentication_failed'), (403, 0, 'authentication_failed'),
    (429, 0, 'rate_limited'), (200, 99991400, 'rate_limited'),
    (400, 230013, 'recipient_unavailable'), (400, 230034, 'invalid_recipient'),
    (400, 230035, 'permission_denied'), (400, 99991663, 'authentication_failed'),
    (400, 12345, 'api_rejected'), (500, 0, 'server_error')])
def test_client_failure_codes(stub, status, code, expected):
    _, responses = stub
    responses.append((status, {'code': code, 'msg': 'FAKE_PRIVATE_RESPONSE_DO_NOT_EXPOSE'}))
    client = AppBot({'FEISHU_APP_ID': 'FAKE_APP', 'FEISHU_APP_SECRET': 'FAKE_SECRET'})
    with pytest.raises(SendError) as error:
        client.send(FAKE_ID, '演示数据', 'FAKE_UUID')
    assert error.value.code == expected and 'FAKE_PRIVATE' not in str(error.value)


def test_cached_token_and_schema(stub):
    calls, responses = stub
    client = AppBot({'FEISHU_APP_ID': 'FAKE_APP', 'FEISHU_APP_SECRET': 'FAKE_SECRET'})
    client.send(FAKE_ID, '演示数据', 'FAKE_UUID_ONE')
    client.send(FAKE_ID, '演示数据', 'FAKE_UUID_TWO')
    assert len([c for c in calls if c['path'] == '/token']) == 1
    responses.append((200, {'code': 0, 'data': {}}))
    with pytest.raises(SendError) as error: client.send(FAKE_ID, '演示数据', 'FAKE_UUID_THREE')
    assert error.value.uncertain and error.value.code == 'invalid_send_response'


def test_retry_limit_and_backoff(configured, browser, db):
    browser.assign()
    class Limited:
        def send(self, *args): raise SendError('rate_limited', retry=True)
    dispatcher = Dispatcher(configured, Limited())
    before = time.time()
    dispatcher.dispatch_one()
    assert row(db)['attempts'] == 1 and row(db)['status'] == 'pending'
    assert row(db)['next_attempt_at'] >= before + 5
    assert not dispatcher.dispatch_one()
    for attempt in (2, 3):
        db.execute('UPDATE notification_outbox SET next_attempt_at=0')
        dispatcher.dispatch_one()
        assert row(db)['attempts'] == attempt
    assert row(db)['status'] == 'failed' and browser.detail()['ticket']['status'] == 'pending_acceptance'


@pytest.mark.parametrize('exception,uncertain', [(requests.ReadTimeout, True), (requests.ConnectTimeout, False), (requests.ConnectionError, True)])
def test_send_timeout_uncertainty_and_no_sensitive_errors(monkeypatch, exception, uncertain):
    client = AppBot({'FEISHU_APP_ID': 'FAKE_APP', 'FEISHU_APP_SECRET': 'FAKE_SECRET'})
    client._token = 'FAKE_TOKEN'; client._expires = time.monotonic() + 7200
    def fail(*args, **kwargs): raise exception('FAKE_SENSITIVE_DETAIL')
    monkeypatch.setattr(requests.Session, 'post', fail)
    with pytest.raises(SendError) as error: client.send(FAKE_ID, '演示数据', 'FAKE_UUID')
    assert error.value.uncertain is uncertain
    assert 'FAKE_SENSITIVE_DETAIL' not in str(error.value)


def test_uncertain_attempt_is_not_automatically_retried(configured, browser, db):
    browser.assign()
    class Uncertain:
        def send(self, *args): raise SendError('send_timeout', uncertain=True)
    dispatcher = Dispatcher(configured, Uncertain())
    dispatcher.dispatch_one()
    assert row(db)['status'] == 'uncertain' and row(db)['attempts'] == 1
    assert not dispatcher.dispatch_one()


def test_mapping_control_and_obsolete_assignment(configured, browser, db, tmp_path):
    binding = tmp_path / 'feishu_bindings.json'
    binding.write_text(json.dumps({'4': {'open_id': 'ou_FAKE_ENGINEER_B_TEST_ONLY', 'enabled': True}}), encoding='utf-8')
    with configured.app_context(): sync_bindings(db, binding)
    assert db.execute('SELECT enabled FROM feishu_bindings WHERE local_user_id=3').fetchone()[0] == 0
    browser.assign(4)
    assert row(db)['status'] == 'pending'
    browser.action('reassign', {'engineer_id': 3, 'priority': '普通', 'priority_confirmed': 'yes', 'reason': '演示改派'})
    class Never:
        def send(self, *args): pytest.fail('Old assignee must not receive a stale message')
    Dispatcher(configured, Never()).dispatch_one()
    assert row(db)['status'] == 'cancelled'
    browser.role(1)
    assert browser.get('/notifications').status_code == 403
    assert browser.post('/notifications', {'open_id': FAKE_ID}).status_code == 405


def test_reset_serializes_send_without_holding_sqlite_writer(configured, browser, db):
    browser.assign(); entered, release = threading.Event(), threading.Event()
    class Blocked:
        def send(self, *args):
            entered.set(); assert release.wait(10)
    with ThreadPoolExecutor(max_workers=2) as pool:
        sending = pool.submit(Dispatcher(configured, Blocked()).dispatch_one)
        assert entered.wait(10)
        # Independent writer is possible while transport waits; the reset gate is in memory.
        db.execute("UPDATE feishu_bindings SET note='test writer can proceed'")
        resetting = pool.submit(reset, Browser(configured))
        try:
            with pytest.raises(TimeoutError): resetting.result(timeout=.2)
        finally: release.set()
        assert sending.result(timeout=10) and resetting.result(timeout=10).status_code == 200
    assert db.execute('SELECT count(*) FROM notification_outbox').fetchone()[0] == 0
    assert db.execute('SELECT count(*) FROM feishu_bindings').fetchone()[0] == 1
    assert not Dispatcher(configured, Blocked()).dispatch_one()


def test_final_generation_gate_no_resurrection(configured, browser, db):
    browser.assign()
    class ExternalReset:
        def send(self, *args):
            connection = connect(configured.config['DATABASE'])
            try:
                connection.execute('BEGIN IMMEDIATE')
                connection.execute('DELETE FROM notification_outbox')
                connection.execute("UPDATE demo_meta SET generation='FAKE_NEW_GENERATION'")
                connection.commit()
            finally: connection.close()
    Dispatcher(configured, ExternalReset()).dispatch_one()
    assert db.execute('SELECT count(*) FROM notification_outbox').fetchone()[0] == 0
    assert db.execute('SELECT verification_status FROM feishu_bindings').fetchone()[0] == 'unverified'


def test_payload_never_sends_unapproved_free_text(configured, browser, db, stub):
    db.execute("UPDATE tickets SET location='FAKE_PRIVATE_LOCATION',issue_description='空调 FAKE_PRIVATE_CONTACT',summary='FAKE_PRIVATE_SUMMARY' WHERE id=1")
    browser.assign(); Dispatcher(configured).dispatch_one()
    text = json.loads(stub[0][1]['body']['content'])['text']
    assert 'FAKE_PRIVATE' not in text and '演示位置（详见本地工单）' in text


def test_restart_sending_is_uncertain_and_worker_lease(configured, browser, db):
    browser.assign()
    db.execute("UPDATE notification_outbox SET status='sending',attempts=1")
    first, second = Dispatcher(configured), Dispatcher(configured)
    first.stop_event.set(); second.stop_event.set()
    try:
        first.start()
        assert row(db)['status'] == 'uncertain' and row(db)['error_code'] == 'process_interrupted'
        with pytest.raises(SendError, match='worker_lock_unavailable'):
            second.start()
    finally:
        first.close(); second.close()
    third = Dispatcher(configured); third.stop_event.set()
    try:
        third.start()
        assert not third.dispatch_one()
    finally: third.close()


def test_invalid_mapping_disables_notifications_without_losing_demo(configured, browser, db, tmp_path, caplog):
    invalid = tmp_path / 'invalid_mapping.json'
    invalid.write_text('FAKE_PRIVATE_INVALID_MAPPING', encoding='utf-8')
    configured.config['FEISHU_BINDINGS_FILE'] = str(invalid)
    dispatcher = Dispatcher(configured)
    try:
        dispatcher.start()
        assert configured.config['FEISHU_ENABLED'] == 'false' and not dispatcher.thread
        browser.assign()
        assert row(db)['status'] == 'disabled'
        assert browser.detail()['ticket']['status'] == 'pending_acceptance'
        assert 'FAKE_PRIVATE_INVALID_MAPPING' not in caplog.text
    finally: dispatcher.close()


@pytest.mark.parametrize('values', [
    {'1': {'open_id': FAKE_ID, 'enabled': True}},
    {'3': {'open_id': 'not-an-open-id', 'enabled': True}},
    {'3': {'open_id': FAKE_ID, 'enabled': 'yes'}}])
def test_invalid_backend_bindings_leave_existing_rows_unchanged(configured, db, tmp_path, values):
    path = tmp_path / 'invalid_mapping.json'
    path.write_text(json.dumps(values), encoding='utf-8')
    before = [tuple(r) for r in db.execute('SELECT * FROM feishu_bindings')]
    with configured.app_context(), pytest.raises(SendError, match='invalid_mapping_config'):
        sync_bindings(db, path)
    assert [tuple(r) for r in db.execute('SELECT * FROM feishu_bindings')] == before


def test_binding_disabled_after_enqueue(configured, browser, db):
    browser.assign()
    db.execute('UPDATE feishu_bindings SET enabled=0')
    class Never:
        def send(self, *args): pytest.fail('Disabled recipient must not be contacted')
    Dispatcher(configured, Never()).dispatch_one()
    assert row(db)['status'] == 'unconfigured'
    assert browser.detail()['ticket']['status'] == 'pending_acceptance'


def test_single_smoke_sends_only_new_ticket_not_older_pending(configured, browser, db, stub, monkeypatch, tmp_path):
    from scripts import feishu_once
    browser.assign()
    old_id = row(db)['id']
    path = tmp_path / 'feishu_bindings.json'
    path.write_text(json.dumps({'3': {'open_id': FAKE_ID, 'enabled': True}}), encoding='utf-8')
    configured.config['FEISHU_BINDINGS_FILE'] = str(path)
    monkeypatch.setattr(feishu_once, 'create_app', lambda *args, **kwargs: configured)
    monkeypatch.setattr('sys.argv', ['feishu_once.py', '--send-one'])
    monkeypatch.chdir(tmp_path)
    assert feishu_once.main() == 0
    assert db.execute('SELECT status FROM notification_outbox WHERE id=?', (old_id,)).fetchone()[0] == 'pending'
    sent = db.execute("SELECT * FROM notification_outbox WHERE status='sent'").fetchall()
    assert len(sent) == 1 and sent[0]['ticket_id'] != 1
    assert len(stub[0]) == 2 # One token request and exactly one message request to loopback.
    evidence = json.loads((tmp_path / 'docs/evidence/stage5-feishu-real.json').read_text(encoding='utf-8'))
    assert evidence['api_accepted'] and not evidence['client_receipt_confirmed'] and evidence['outbox_attempts'] == 1


def test_single_smoke_missing_mapping_never_calls_api(configured, db, stub, monkeypatch, tmp_path):
    from scripts import feishu_once
    db.execute('UPDATE feishu_bindings SET enabled=0')
    monkeypatch.setattr(feishu_once, 'create_app', lambda *args, **kwargs: configured)
    monkeypatch.setattr('sys.argv', ['feishu_once.py', '--send-one'])
    monkeypatch.chdir(tmp_path)
    assert feishu_once.main() == 1 and not stub[0]
    assert db.execute('SELECT count(*) FROM tickets').fetchone()[0] == 1


def bind_service(db):
    db.execute("INSERT INTO feishu_bindings(local_user_id,open_id,enabled) VALUES(2,'ou_FAKE_SERVICE_TEST_ONLY',1)")


def test_completion_http_stub_targets_dispatching_service(configured, browser, db, stub):
    bind_service(db)
    browser.review()
    job = db.execute("SELECT * FROM notification_outbox WHERE kind='completed'").fetchone()
    assert job['target_user_id'] == 2 and job['status'] == 'pending'
    assert Dispatcher(configured).dispatch_one(job['id'])
    assert json.loads(stub[0][1]['body']['content'])['text'].startswith('【演示数据 · 完工待企服验收】')
    assert stub[0][1]['body']['receive_id'] == 'ou_FAKE_SERVICE_TEST_ONLY'
    assert '处理灯具异常' not in stub[0][1]['body']['content']
    assert browser.detail()['ticket']['status'] == 'pending_service_review'
    assert all(n['target_user_id'] == 3 for n in browser.get('/notifications').json['notifications'])


def test_missing_reviewer_mapping_does_not_send_to_engineer(configured, browser, db):
    browser.review()
    job = db.execute("SELECT * FROM notification_outbox WHERE kind='completed'").fetchone()
    assert job['status'] == 'unconfigured' and job['target_user_id'] == 2
    assert browser.detail()['ticket']['status'] == 'pending_service_review'
    assert browser.complete().status_code == 409
    assert db.execute("SELECT count(*) FROM notification_outbox WHERE kind='completed'").fetchone()[0] == 1


def test_old_review_round_does_not_become_relevant_again(configured, browser, db, stub):
    bind_service(db); browser.review()
    old = db.execute("SELECT id FROM notification_outbox WHERE kind='completed'").fetchone()[0]
    browser.role(2); assert browser.action('reject', {'reason': '演示补充'}).status_code == 200
    browser.role(3); assert browser.action('resume').status_code == 200
    assert browser.complete().status_code == 200
    new = db.execute("SELECT id FROM notification_outbox WHERE kind='completed' AND id<>?", (old,)).fetchone()[0]
    dispatcher = Dispatcher(configured)
    assert dispatcher.dispatch_one(old) and not stub[0]
    assert db.execute('SELECT status FROM notification_outbox WHERE id=?', (old,)).fetchone()[0] == 'cancelled'
    assert dispatcher.dispatch_one(new) and len(stub[0]) == 2


def test_assignment_back_to_same_engineer_cancels_old_round(configured, browser, db):
    browser.assign(); old = row(db)['id']
    for engineer in (4, 3):
        assert browser.action('reassign', {'engineer_id': engineer, 'priority': '普通',
            'priority_confirmed': 'yes', 'reason': '演示轮次调整'}).status_code == 200
    class Never:
        def send(self, *args): pytest.fail('Must not send an obsolete assignment round')
    assert Dispatcher(configured, Never()).dispatch_one(old)
    assert db.execute('SELECT status FROM notification_outbox WHERE id=?', (old,)).fetchone()[0] == 'cancelled'


def test_completion_once_preserves_assignment_and_refuses_replay(configured, browser, db, stub, monkeypatch, tmp_path):
    from scripts import feishu_once
    bind_service(db); browser.assign()
    assigned = row(db)['id']; Dispatcher(configured).dispatch_one(assigned)
    mapping = tmp_path / 'feishu_bindings.json'
    mapping.write_text(json.dumps({'3': {'open_id': FAKE_ID, 'enabled': True},
        '2': {'open_id': 'ou_FAKE_SERVICE_TEST_ONLY', 'enabled': True}}), encoding='utf-8')
    configured.config['FEISHU_BINDINGS_FILE'] = str(mapping)
    photo = tmp_path / 'tests/fixtures/demo-placeholder.png'
    photo.parent.mkdir(parents=True); photo.write_bytes(image_bytes())
    monkeypatch.setattr(feishu_once, 'create_app', lambda *args, **kwargs: configured)
    monkeypatch.setattr('sys.argv', ['feishu_once.py', '--send-one', '--event', 'completed', '--ticket-id', '1'])
    monkeypatch.chdir(tmp_path)
    assert feishu_once.main() == 0
    assert browser.detail()['ticket']['status'] == 'pending_service_review'
    assert db.execute('SELECT attempts FROM notification_outbox WHERE id=?', (assigned,)).fetchone()[0] == 1
    count = len(stub[0])
    assert feishu_once.main() == 1 and len(stub[0]) == count
    evidence = json.loads((tmp_path / 'docs/evidence/stage5-feishu-completion-real.json').read_text(encoding='utf-8'))
    assert evidence['api_accepted'] # A rejected replay must preserve the original success evidence.
    rejected = next((tmp_path / 'docs/evidence').glob('stage5-feishu-completion-real-attempt-*.json'))
    assert json.loads(rejected.read_text(encoding='utf-8'))['error_code'] == 'completion_already_tested'


def test_mapping_reload_preserves_verification_only_for_same_recipient(configured, db, tmp_path):
    db.execute("UPDATE feishu_bindings SET verification_status='api_accepted',last_verified_at=123")
    path = tmp_path / 'feishu_bindings.json'
    path.write_text(json.dumps({'3': {'open_id': FAKE_ID, 'enabled': True}}), encoding='utf-8')
    with configured.app_context(): sync_bindings(db, path)
    assert db.execute('SELECT verification_status,last_verified_at FROM feishu_bindings').fetchone()[:] == ('api_accepted', 123)
    path.write_text(json.dumps({'3': {'open_id': 'ou_FAKE_REPLACEMENT_ENGINEER', 'enabled': True}}), encoding='utf-8')
    with configured.app_context(): sync_bindings(db, path)
    assert db.execute('SELECT verification_status,last_verified_at FROM feishu_bindings').fetchone()[:] == ('unverified', None)


def test_notice_describes_actual_integration_mode(configured, browser):
    browser.role(2)
    html = browser.client.get('/workspace').get_data(as_text=True)
    assert '飞书应用通知已启用' in html and '通知状态（API记录）' in html
    configured.config['FEISHU_ENABLED'] = 'false'
    html = browser.client.get('/workspace').get_data(as_text=True)
    assert '仅站内模拟推送，无外部通知' in html and '通知状态（API记录）' not in html
