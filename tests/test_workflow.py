import io
import json
import sqlite3
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest
from werkzeug.exceptions import Conflict, Forbidden

from park_service import create_app
from park_service.db import connect
from park_service.workflow import apply_action, STATES
from .conftest import Browser, image_bytes

FORM = dict(reporter_name='演示报修甲', reporter_contact='DEMO-分机', company_name='',
            location='演示公共茶水间', original_text='演示：灯不亮', issue_description='演示：灯不亮')


def snapshot(db):
    return {table: [tuple(row) for row in db.execute(f'SELECT * FROM {table} ORDER BY id')]
            for table in ['tickets', 'ticket_events', 'assignments', 'completion_submissions', 'attachments']}


def test_T01_create_and_T29_seed(browser, db):
    assert db.execute('SELECT count(*) FROM users').fetchone()[0] == 6
    assert db.execute('SELECT count(*) FROM tickets').fetchone()[0] == 1
    assert all(row[0] == 1 for row in db.execute('SELECT is_demo FROM users'))
    response = browser.post('/tickets', FORM)
    assert response.status_code == 201
    ticket = browser.detail(response.json['id'])['ticket']
    assert ticket['status'] == 'pending_assignment' and ticket['priority'] is None
    assert ticket['location'] == FORM['location'] and ticket['original_text'] == FORM['original_text']
    assert ticket['number'] != browser.detail(1)['ticket']['number']
    assert len(browser.detail(ticket['id'])['events']) == 1
    html = browser.client.get('/tickets').get_data(as_text=True)
    assert '演示数据' in html and '当前仅手工填写' in html


@pytest.mark.parametrize('key,limit', [('reporter_name',50),('reporter_contact',100),('location',200),('original_text',2000),('issue_description',2000)])
@pytest.mark.parametrize('kind', ['missing','blank','long'])
def test_T02_required(browser, db, key, limit, kind):
    data = FORM.copy()
    if kind == 'missing': data.pop(key)
    else: data[key] = '   ' if kind == 'blank' else '字' * (limit+1)
    before = snapshot(db)
    assert browser.post('/tickets', data).status_code == 400
    assert snapshot(db) == before


@pytest.mark.parametrize('overrides', [dict(priority_confirmed=''),dict(priority='不合法'),dict(engineer_id='999'),dict(engineer_id='2')])
def test_T03_assignment_validation(browser, db, overrides):
    browser.role(2)
    before = snapshot(db)
    data = dict(engineer_id='3',priority='普通',priority_confirmed='yes')
    data.update(overrides)
    assert browser.action('assign', data).status_code == 400
    assert snapshot(db) == before


def test_T04_close_and_T15_persistent(app, browser, db):
    browser.review()
    browser.role(2)
    assert browser.action('close', {'review_confirmed':'yes'}).status_code == 200
    detail = browser.detail()
    assert [e['to_status'] for e in detail['events']] == [
        'pending_assignment','pending_acceptance','in_progress','pending_service_review','closed']
    assert detail['ticket']['closed_at']
    events = list(db.execute('SELECT * FROM ticket_events'))
    assert all(e['actor_role'] and e['before_values'] and e['after_values'] and e['effective_at'] for e in events)
    assert detail['events'][-1]['actor_role'] == 'service'
    assert set(STATES) == {'pending_assignment','pending_acceptance','in_progress','pending_service_review','returned','closed'}
    app2 = create_app(dict(TESTING=True, SECRET_KEY='not-a-credential-test-session',
        DATABASE=app.config['DATABASE'], UPLOAD_ROOT=app.config['UPLOAD_ROOT']))
    other = Browser(app2); other.role(2)
    assert other.detail()['ticket'] == detail['ticket']
    assert other.detail()['completions'] == detail['completions']
    for value in ['待企服验收','not_a_state']:
        with pytest.raises(sqlite3.IntegrityError):
            db.execute('UPDATE tickets SET status=? WHERE id=1', (value,))


def test_T05_reject_new_round(browser, db):
    browser.review(); browser.role(2)
    before = snapshot(db)
    assert browser.action('reject', {'reason':'   '}).status_code == 400
    assert snapshot(db) == before
    assert browser.action('reject', {'reason':'演示：补充复核'}).status_code == 200
    browser.role(3)
    assert browser.complete().status_code == 409
    assert browser.action('resume').status_code == 200
    assert browser.action('submit_completion', {'resolution_note':'第二轮'}).status_code == 400
    assert browser.complete().status_code == 200
    data=browser.detail()
    assert len(data['completions']) == 2
    assert data['completions'][0]['reject_reason'] == '演示：补充复核'
    assert data['completions'][0]['review_result'] == 'rejected'
    assert data['completions'][0]['photos'][0]['id'] != data['completions'][1]['photos'][0]['id']
    assert db.execute('SELECT count(*) FROM attachments').fetchone()[0] == 2
    browser.role(2)
    assert browser.action('close', {'review_confirmed':'yes'}).status_code == 200


def test_T06_reassign_history(browser, db):
    browser.assign()
    before=snapshot(db)
    data=dict(engineer_id='4',priority='普通',priority_confirmed='yes',reason='')
    assert browser.action('reassign',data).status_code == 400
    assert snapshot(db)==before
    data.update(engineer_id='3',reason='演示调整')
    assert browser.action('reassign',data).status_code == 400
    data['engineer_id']='4'
    assert browser.action('reassign',data).status_code == 200
    detail=browser.detail()
    assert len(detail['assignments'])==2 and detail['assignments'][0]['ended_at']
    assert detail['assignments'][0]['pushed_at'] != detail['assignments'][1]['pushed_at']
    browser.role(3)
    assert browser.get('/tickets/1').status_code==403
    assert browser.action('accept',version=3).status_code==403
    browser.role(4)
    assert browser.action('accept').status_code==200
    browser.role(2)
    assert browser.action('reassign',data).status_code==409


def test_edit_and_update_assignment(browser, db):
    browser.role(2)
    data={**FORM,'location':'演示大厅'}
    data.pop('original_text')
    assert browser.action('edit',data).status_code==200
    assert browser.detail()['ticket']['original_text']=='演示：公共走廊灯不亮。'
    assert browser.action('edit',{**data,'original_text':'替换原文'}).status_code==400
    browser.assign()
    assignment=browser.detail()['assignments'][0]
    assert browser.action('update_assignment',dict(priority='特急',priority_confirmed='yes',due_at='2026-11-13T10:00',reason='演示人工设置')).status_code==200
    ticket=browser.detail()['ticket']
    assert ticket['due_at'].startswith('2026-11-13T02:00')
    assert browser.detail()['assignments'][0]==assignment
    assert browser.action('update_assignment',dict(priority='特急',priority_confirmed='yes',due_at='',reason='演示清空')).status_code==200
    assert browser.detail()['ticket']['due_at'] is None
    assert '截止时间未配置' in browser.client.get('/tickets/1').get_data(as_text=True)


@pytest.mark.parametrize('uid',[1,3,4,5,6])
def test_T07_unauthorized_mutations(browser, db, uid):
    browser.review(); photo=browser.detail()['completions'][0]['photos'][0]['id']
    browser.role(uid)
    before=snapshot(db)
    assert browser.action('close',{'review_confirmed':'yes'},version=4).status_code==403
    if uid in (4,6):
        assert browser.get('/tickets/1').status_code==403
        assert browser.get(f'/attachments/{photo}').status_code==403
        assert browser.get('/tickets').json['tickets']==[]
    assert snapshot(db)==before


def test_T08_csrf_forgery(browser, db):
    before=snapshot(db)
    assert browser.client.post('/role',data={'user_id':'2'}).status_code==400
    assert browser.post('/role',{'user_id':'999'}).status_code==400
    assert browser.action('assign',{'role':'service','actor_id':'2'},version=1).status_code==403
    assert snapshot(db)==before
    with browser.client.session_transaction() as session:
        session['actor_id']=999
    assert browser.get('/tickets').status_code==403


@pytest.mark.parametrize('state,action,uid',[(state,action,uid) for action,uid,allowed in [
    ('assign',2,'pending_assignment'),('reassign',2,'pending_acceptance'),
    ('update_assignment',2,'pending_acceptance'),('edit',2,'pending_assignment'),
    ('accept',3,'pending_acceptance'),('submit_completion',3,'in_progress'),
    ('reject',2,'pending_service_review'),('resume',3,'returned'),('close',2,'pending_service_review')]
    for state in STATES if state!=allowed])
def test_T09_all_illegal_edges(browser,db,state,action,uid):
    db.execute('UPDATE tickets SET status=?,assigned_engineer_id=3 WHERE id=1',(state,))
    browser.role(uid); before=snapshot(db)
    assert browser.action(action,version=1).status_code==409
    assert snapshot(db)==before


def test_T09_closed_immutable(browser,db):
    browser.review(); browser.role(2)
    assert browser.action('close',{'review_confirmed':'yes'}).status_code==200
    before=snapshot(db)
    for action in ['close','reject','assign','reassign','edit','update_assignment']:
        assert browser.action(action,dict(reason='演示',review_confirmed='yes'),version=5).status_code==409
    browser.role(3)
    for action in ['accept','resume','submit_completion']:
        assert browser.action(action,version=5).status_code==409
    assert snapshot(db)==before


@pytest.mark.parametrize('action',['accept','close','submit_completion'])
def test_T10_concurrent_real_connections(app,browser,db,action):
    if action=='accept': browser.assign(); uid=3
    elif action=='close': browser.review(); uid=2
    else: browser.processing(); uid=3
    version=browser.detail()['ticket']['version']; before_count=db.execute('SELECT count(*) FROM ticket_events').fetchone()[0]
    actor=dict(db.execute('SELECT * FROM users WHERE id=?',(uid,)).fetchone())
    barrier=threading.Barrier(2)
    def worker():
        connection=connect(app.config['DATABASE'])
        try:
            barrier.wait(timeout=10)
            data=dict(version=str(version),review_confirmed='yes',resolution_note='演示并发提交')
            from werkzeug.datastructures import FileStorage
            photos=[FileStorage(io.BytesIO(image_bytes()),filename='demo.png',content_type='image/png')] if action=='submit_completion' else []
            apply_action(connection,app.config['UPLOAD_ROOT'],actor,1,action,data,photos)
            return 200
        except Conflict:
            return 409
        finally:
            connection.close()
    with ThreadPoolExecutor(max_workers=2) as pool:
        results=list(pool.map(lambda _:worker(),range(2)))
    assert sorted(results)==[200,409]
    assert db.execute('SELECT count(*) FROM ticket_events').fetchone()[0]==before_count+1
    assert db.execute('SELECT version FROM tickets WHERE id=1').fetchone()[0]==version+1
    if action=='submit_completion':
        assert db.execute('SELECT count(*) FROM completion_submissions').fetchone()[0]==1
        assert len(list(Path(app.config['UPLOAD_ROOT']).iterdir()))==1


def test_T10_stale_repeat(browser, db):
    browser.assign(); browser.role(3)
    assert browser.action('accept',version=2).status_code==200
    before=snapshot(db)
    assert browser.action('accept',version=2).status_code==409
    assert snapshot(db)==before


def test_T10_create_duplicate_is_not_new_event(browser, db):
    session = browser.client.get('/session').json
    data = {**FORM, 'create_nonce': session['create_nonce']}
    headers = {'Accept':'application/json','X-CSRF-Token':browser.token}
    assert browser.client.post('/tickets', data=data, headers=headers).status_code==201
    before=snapshot(db)
    assert browser.client.post('/tickets', data=data, headers=headers).status_code==409
    assert snapshot(db)==before


def test_T10_reassign_accept_race(app,browser,db):
    browser.assign()
    barrier=threading.Barrier(2)
    count=db.execute('SELECT count(*) FROM ticket_events').fetchone()[0]
    def worker(action):
        connection=connect(app.config['DATABASE'])
        try:
            actor=dict(connection.execute('SELECT * FROM users WHERE id=?',(2 if action=='reassign' else 3,)).fetchone())
            barrier.wait(timeout=10)
            apply_action(connection,app.config['UPLOAD_ROOT'],actor,1,action,
                dict(version='2',engineer_id='4',priority='普通',priority_confirmed='yes',reason='演示并发改派'))
            return 200
        except (Conflict,Forbidden) as e:
            return e.code
        finally: connection.close()
    with ThreadPoolExecutor(max_workers=2) as pool:
        result=list(pool.map(worker,['reassign','accept']))
    assert result.count(200)==1 and all(code in (200,403,409) for code in result)
    assert db.execute('SELECT count(*) FROM ticket_events').fetchone()[0]==count+1
    ticket=dict(db.execute('SELECT * FROM tickets WHERE id=1').fetchone())
    assert (ticket['status'],ticket['assigned_engineer_id']) in [('pending_acceptance',4),('in_progress',3)]


def test_T11_empty_note_with_valid_photo(browser,db):
    browser.processing(); before=snapshot(db)
    assert browser.action('submit_completion', {'resolution_note':' ',
        'photos':(io.BytesIO(image_bytes()),'demo.png','image/png')}).status_code==400
    assert snapshot(db)==before


@pytest.mark.parametrize('data', [{},{'resolution_note':' '},{'resolution_note':'演示'},{'resolution_note':'演示','attachment_id':'1'},{'resolution_note':'演示','submission_id':'1'}])
def test_T11_required_evidence(browser,db,data):
    browser.processing(); before=snapshot(db)
    assert browser.action('submit_completion',data).status_code==400
    assert snapshot(db)==before


def test_T15_event_failure_rolls_back_and_cleans(app,browser,db,monkeypatch):
    browser.processing(); before=snapshot(db)
    import park_service.workflow as wf
    def fail(*args,**kwargs): raise sqlite3.IntegrityError('simulated storage failure')
    monkeypatch.setattr(wf,'event',fail)
    assert browser.complete().status_code==500
    assert snapshot(db)==before
    assert list(Path(app.config['UPLOAD_ROOT']).iterdir())==[]


def test_T15_file_failure_rolls_back(app,browser,db,monkeypatch):
    browser.processing(); before=snapshot(db)
    import park_service.files as fs
    def fail(path,data):
        path.write_bytes(b'partial')
        raise OSError('simulated disk full')
    monkeypatch.setattr(fs,'write_photo',fail)
    assert browser.complete().status_code==500
    assert snapshot(db)==before
    assert list(Path(app.config['UPLOAD_ROOT']).iterdir())==[]


def test_T15_create_event_failure(browser,db,monkeypatch):
    import park_service.workflow as wf
    before=snapshot(db)
    def fail(*args,**kwargs): raise sqlite3.IntegrityError('simulated')
    monkeypatch.setattr(wf,'event',fail)
    assert browser.post('/tickets',FORM).status_code==500
    assert snapshot(db)==before


@pytest.mark.parametrize('action',['assign','accept','close'])
def test_T15_transition_event_failure_is_atomic(browser,db,monkeypatch,action):
    import park_service.workflow as wf
    if action=='assign': browser.role(2)
    elif action=='accept': browser.assign(); browser.role(3)
    else: browser.review(); browser.role(2)
    before=snapshot(db)
    def fail(*args,**kwargs): raise sqlite3.IntegrityError('simulated event failure')
    monkeypatch.setattr(wf,'event',fail)
    result=browser.action(action,dict(engineer_id='3',priority='普通',priority_confirmed='yes',review_confirmed='yes'))
    assert result.status_code==500
    assert snapshot(db)==before


def test_T10_concurrent_creation(app,db):
    from park_service.workflow import create_ticket
    actor=dict(db.execute('SELECT * FROM users WHERE id=1').fetchone())
    barrier=threading.Barrier(2)
    before=db.execute('SELECT count(*) FROM tickets').fetchone()[0]
    def worker(_):
        connection=connect(app.config['DATABASE'])
        try:
            barrier.wait(timeout=10)
            create_ticket(connection,actor,FORM,request_id='same-demo-create-operation')
            return 201
        except Conflict: return 409
        finally: connection.close()
    with ThreadPoolExecutor(max_workers=2) as pool:
        assert sorted(pool.map(worker,range(2)))==[201,409]
    assert db.execute('SELECT count(*) FROM tickets').fetchone()[0]==before+1
    assert db.execute("SELECT count(*) FROM ticket_events WHERE request_id='same-demo-create-operation'").fetchone()[0]==1


def test_T16_escape_and_no_traceback(browser):
    response=browser.post('/tickets',{**FORM,'issue_description':'<script>alert(1)</script>'})
    html=browser.client.get(f"/tickets/{response.json['id']}").get_data(as_text=True)
    assert '<script>alert(1)</script>' not in html
    assert '&lt;script&gt;' in html
    error=browser.client.post('/role',data={'user_id':'2'}).get_data(as_text=True)
    assert '操作校验失败' in error and 'Traceback' not in error


def test_manager_masks_contacts_and_is_readonly(browser):
    response=browser.post('/tickets',{**FORM,'reporter_contact':'DEMO-PRIVATE-CONTACT','original_text':'DEMO-PRIVATE-CONTACT'})
    browser.role(5)
    detail=browser.get(f"/tickets/{response.json['id']}")
    assert 'DEMO-PRIVATE-CONTACT' not in detail.get_data(as_text=True)
    html=browser.client.get(f"/tickets/{response.json['id']}").get_data(as_text=True)
    assert 'DEMO-PRIVATE-CONTACT' not in html and '验收关闭</button>' not in html
