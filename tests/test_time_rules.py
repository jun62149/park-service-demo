from datetime import datetime,timedelta,timezone

import pytest

from park_service import clock
from .conftest import Browser


@pytest.fixture
def timed(app,browser,monkeypatch):
    base=[datetime(2027,1,1,tzinfo=timezone.utc)]
    monkeypatch.setattr(clock,'real_now',lambda:base[0])
    return app,browser,base


def urgent(browser):
    browser.role(2)
    assert browser.action('assign',dict(engineer_id='3',priority='特急',priority_confirmed='yes')).status_code==200


def flags(browser): return browser.get('/reminders').json['flags']['1']


@pytest.mark.parametrize('seconds,expected',[(299,False),(300,True),(301,True)])
def test_T22_threshold(timed,db,seconds,expected):
    app,b,base=timed; urgent(b); base[0]+=timedelta(seconds=seconds)
    assert flags(b)['phone_followup'] is expected
    assert db.execute("SELECT count(*) FROM reminders WHERE kind='phone'").fetchone()[0]==int(expected)
    assert 'phone_followup' in flags(b)


def test_T23_nonurgent_no_push_accepted_and_due_empty(timed,db):
    app,b,base=timed; b.assign(); base[0]+=timedelta(seconds=500)
    assert not flags(b)['phone_followup'] and not flags(b)['overdue']
    db.execute("UPDATE tickets SET priority='特急' WHERE id=1")
    # pushed_at is NOT NULL by schema. A missing assignment must never infer a start.
    assignment=db.execute('SELECT current_assignment_id FROM tickets WHERE id=1').fetchone()[0]
    db.execute('UPDATE tickets SET current_assignment_id=NULL WHERE id=1')
    assert not flags(b)['phone_followup']
    db.execute('UPDATE tickets SET current_assignment_id=? WHERE id=1',(assignment,))
    assert flags(b)['phone_followup']
    b.role(3); assert b.action('accept').status_code==200
    assert not flags(b)['phone_followup']


def test_T24_reassign_and_escalate(timed,db):
    app,b,base=timed; urgent(b); base[0]+=timedelta(seconds=300)
    assert flags(b)['phone_followup']
    assert b.action('reassign',dict(engineer_id='4',priority='特急',priority_confirmed='yes',reason='演示改派')).status_code==200
    assert not flags(b)['phone_followup']
    base[0]+=timedelta(seconds=299); assert not flags(b)['phone_followup']
    base[0]+=timedelta(seconds=1); assert flags(b)['phone_followup']
    assert db.execute("SELECT count(*) FROM reminders WHERE kind='phone'").fetchone()[0]==2
    assignment=b.detail()['assignments'][-1]
    assert b.action('update_assignment',dict(priority='普通',priority_confirmed='yes',reason='演示降级')).status_code==200
    assert not flags(b)['phone_followup']
    assert b.action('update_assignment',dict(priority='特急',priority_confirmed='yes',reason='演示升为特急')).status_code==200
    assert flags(b)['phone_followup'] and b.detail()['assignments'][-1]==assignment


@pytest.mark.parametrize('seconds,overdue',[(-1,False),(0,False),(1,True)])
def test_T25_due_boundary(timed,db,seconds,overdue):
    app,b,base=timed; b.assign()
    db.execute('UPDATE tickets SET due_at=? WHERE id=1',((base[0]-timedelta(seconds=seconds)).isoformat(),))
    assert flags(b)['overdue'] is overdue


def test_T25_pending_review_and_closed_history(timed,db):
    app,b,base=timed; b.review()
    db.execute('UPDATE tickets SET due_at=? WHERE id=1',((base[0]-timedelta(seconds=1)).isoformat(),))
    assert flags(b)['overdue']
    b.role(2); assert b.action('close',{'review_confirmed':'yes'}).status_code==200
    result=flags(b)
    assert not result['overdue'] and result['historical_late_closed']


def test_T26_due_clear_and_version_scope(timed,db):
    app,b,base=timed; b.assign()
    assert flags(b)['deadline_unconfigured'] and not flags(b)['overdue']
    for suffix in [1,2]:
        due=(base[0]-timedelta(seconds=suffix)).isoformat()
        assert b.action('update_assignment',dict(priority='加急',priority_confirmed='yes',due_at=due,reason='演示调整截止')).status_code==200
        assert flags(b)['overdue']
    assert db.execute("SELECT count(*) FROM reminders WHERE kind='overdue'").fetchone()[0]==2
    assert b.action('update_assignment',dict(priority='加急',priority_confirmed='yes',due_at='',reason='演示清空')).status_code==200
    assert flags(b)['deadline_unconfigured'] and not flags(b)['overdue']
    assert '无法判断是否超期' in b.client.get('/tickets/1').get_data(as_text=True)


def test_T27_dedupe_and_reboot(timed,db):
    app,b,base=timed; urgent(b); base[0]+=timedelta(seconds=301)
    for _ in range(4): assert flags(b)['phone_followup']
    first=dict(db.execute("SELECT * FROM reminders WHERE kind='phone'").fetchone())
    from park_service import create_app
    app2=create_app(dict(TESTING=True,SECRET_KEY='another-test-only-session',DATABASE=app.config['DATABASE'],UPLOAD_ROOT=app.config['UPLOAD_ROOT']))
    b2=Browser(app2); b2.role(2); assert flags(b2)['phone_followup']
    assert db.execute("SELECT count(*) FROM reminders WHERE kind='phone'").fetchone()[0]==1
    assert dict(db.execute("SELECT * FROM reminders WHERE kind='phone'").fetchone())['first_detected_at']==first['first_detected_at']
    b.role(3); assert b.action('accept').status_code==200
    flags(b)
    assert db.execute("SELECT resolved_at FROM reminders WHERE kind='phone'").fetchone()[0]


def test_T28_demo_clock_shared_with_workflow_and_privacy(timed,db):
    app,b,base=timed; urgent(b)
    real=base[0]
    assert b.post('/demo/advance',{'seconds':'300'}).status_code==200
    assert base[0]==real and flags(b)['phone_followup']
    b.role(3); assert b.action('accept').status_code==200
    ticket=b.detail()['ticket']
    assert datetime.fromisoformat(ticket['accepted_at'])==real+timedelta(seconds=300)
    last=db.execute('SELECT effective_at,real_recorded_at FROM ticket_events ORDER BY id DESC LIMIT 1').fetchone()
    assert datetime.fromisoformat(last['effective_at'])>=datetime.fromisoformat(last['real_recorded_at'])
    b.role(2)
    app.config['DEEPSEEK_API_KEY']=''
    d=b.post('/parse',{'source_text':'演示：灯不亮'}).json
    assert b.post('/parse/confirm',{**d,'acknowledge':'yes'}).json['confirmed']
    assert b.post('/demo/advance',{'seconds':'86400'}).status_code==200
    r=b.post('/parse/extract',d).json
    assert r['fallback_reason']=='missing_key' # Fast-forward never expires real-time TTL.
    assert '演示时间 +' in b.client.get('/tickets').get_data(as_text=True)
    assert b.post('/demo/advance',{'seconds':'0'}).status_code==400
    assert b.post('/demo/advance',{'seconds':'-300'}).status_code==400
    assert b.post('/demo/reset',{}).status_code==404 # Reset belongs to stage 3.
    app.config['DEMO_MODE']=False
    assert b.post('/demo/advance',{'seconds':'1'}).status_code==403


def test_T28_clock_permission_and_T27_visibility(timed):
    app,b,base=timed
    assert b.post('/demo/advance',{'seconds':'300'}).status_code==403
    urgent(b); base[0]+=timedelta(seconds=300)
    b.role(4)
    assert b.get('/reminders').json['flags']=={}
