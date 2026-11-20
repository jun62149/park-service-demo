import re
from pathlib import Path
import pytest
from .conftest import Browser


def token(browser):
    r=browser.client.get('/demo/reset-data')
    assert r.status_code==200
    return re.search(r'name="reset_token" value="([^"]+)"',r.get_data(as_text=True)).group(1)


def test_T30_metrics_and_permissions(browser,db):
    browser.role(2)
    r=browser.get('/workspace').json
    assert r['metrics']['total']==1 and sum(r['metrics']['states'].values())==1
    assert r['metrics']['close_rate']==0 and r['metrics']['unconfigured']==1
    browser.review(); browser.role(2)
    assert browser.action('close',{'review_confirmed':'yes'}).status_code==200
    assert browser.get('/workspace').json['metrics']['close_rate']==100
    browser.role(4)
    assert browser.get('/workspace').json['visible_ids']==[]
    browser.role(5)
    html=browser.client.get('/workspace').get_data(as_text=True)
    assert '管理工作台' in html and 'DEMO-前台分机' not in html
    assert '演示关闭率' in html


def test_T31_reset_atomic_clock_history_and_stale_sessions(app,browser,db):
    browser.review(); browser.role(2)
    photo=browser.detail()['completions'][0]['photos'][0]['id']
    unrelated=Path(app.config['UPLOAD_ROOT'])/'keep-unrelated.txt'; unrelated.write_text('demo')
    old=Browser(app)
    browser.post('/demo/advance',{'seconds':'500'})
    nonce=token(browser)
    r=browser.post('/demo/reset-data',{'reset_token':nonce,'confirmation':'重置演示数据'})
    assert r.status_code==200 and r.json['clock_reset']
    assert db.execute('SELECT offset_seconds FROM demo_clock').fetchone()[0]==0
    assert db.execute('SELECT count(*) FROM tickets').fetchone()[0]==3
    assert db.execute('SELECT min(id) FROM tickets').fetchone()[0]>1
    assert db.execute('SELECT count(*) FROM attachments').fetchone()[0]==0
    assert browser.get(f'/attachments/{photo}').status_code==404
    assert old.post('/tickets',{}).status_code==400 # Old CSRF/session invalidated globally.
    assert browser.get('/tickets/1').status_code==404
    assert db.execute('PRAGMA foreign_key_check').fetchall()==[]
    assert list(Path(app.config['UPLOAD_ROOT']).iterdir())==[unrelated]


@pytest.mark.parametrize('uid',[1,3,4,5])
def test_T31_reset_unauthorized(browser,uid):
    browser.role(uid)
    assert browser.client.get('/demo/reset-data').status_code==403
    assert browser.post('/demo/reset-data',{'confirmation':'重置演示数据'}).status_code==403


def test_T31_confirmation_offswitch_rollback(app,browser,db,monkeypatch):
    browser.role(2); nonce=token(browser)
    assert browser.post('/demo/reset-data',{'reset_token':nonce,'confirmation':'wrong'}).status_code==400
    import park_service.workspaces as w
    def fail(*a): raise RuntimeError('simulated seed failure')
    monkeypatch.setattr(w,'seed_examples',fail)
    assert browser.post('/demo/reset-data',{'reset_token':nonce,'confirmation':'重置演示数据'}).status_code==500
    assert db.execute('SELECT count(*) FROM tickets').fetchone()[0]==1
    app.config['DEMO_MODE']=False
    assert browser.client.get('/demo/reset-data').status_code==403


def test_T30_empty_metrics():
    from park_service.workspaces import metrics
    r=metrics([],{})
    assert r['total']==0 and r['close_rate'] is None and sum(r['states'].values())==0
