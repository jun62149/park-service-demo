"""Focused S4-M01 completion-boundary regression. All providers are local stubs."""
import json
import re
import threading
from concurrent.futures import ThreadPoolExecutor, TimeoutError

import pytest

from park_service import ai
from park_service.db import get_db
from .conftest import Browser

OLD_TEXT='演示修复验证旧原文；禁止写入冲突日志。'


def prepare(app):
    app.config.update(AI_PROVIDER='deepseek',DEEPSEEK_API_KEY='fix_simulation_marker')
    browser=Browser(app)
    draft=browser.post('/parse',{'source_text':OLD_TEXT}).json
    assert browser.post('/parse/confirm',{**draft,'acknowledge':'yes'}).json['confirmed']
    return browser,draft


def wire():
    return {'choices':[{'finish_reason':'stop','message':{'content':json.dumps({k:None for k in ai.KEYS})}}]}


def reset(browser):
    browser.role(2)
    html=browser.client.get('/demo/reset-data').get_data(as_text=True)
    nonce=re.search(r'name="reset_token" value="([^"]+)"',html).group(1)
    response=browser.post('/demo/reset-data',{'reset_token':nonce,'confirmation':'重置演示数据'})
    assert response.status_code==200


def test_normal_model_completion_still_succeeds(app,db,monkeypatch):
    b,d=prepare(app)
    monkeypatch.setattr(ai,'transport',lambda *args:wire())
    response=b.post('/parse/extract',d)
    assert response.status_code==200 and response.json['provider']=='deepseek'
    assert db.execute('SELECT count(*) FROM extraction_runs').fetchone()[0]==1


@pytest.mark.parametrize('mutation',['generation','delete_draft','version','owner','actor','text'])
def test_completion_rechecks_every_binding(app,db,monkeypatch,caplog,mutation):
    b,d=prepare(app); entered=threading.Event(); release=threading.Event()
    def model(*args):
        assert not get_db().in_transaction
        entered.set(); assert release.wait(10); return wire()
    monkeypatch.setattr(ai,'transport',model)
    with ThreadPoolExecutor(max_workers=1) as pool:
        pending=pool.submit(b.post,'/parse/extract',d)
        try:
            assert entered.wait(10)
            if mutation=='generation': db.execute("UPDATE demo_meta SET generation='new-test-generation'")
            elif mutation=='delete_draft':
                db.execute('DELETE FROM parse_previews WHERE draft_id=?',(d['draft_id'],))
                db.execute('DELETE FROM parse_drafts WHERE id=?',(d['draft_id'],))
            elif mutation=='version': db.execute('UPDATE parse_drafts SET version=version+1 WHERE id=?',(d['draft_id'],))
            elif mutation=='owner': db.execute("UPDATE parse_drafts SET owner_session='different-owner' WHERE id=?",(d['draft_id'],))
            elif mutation=='actor': db.execute('UPDATE parse_drafts SET actor_id=6 WHERE id=?',(d['draft_id'],))
            else: db.execute("UPDATE parse_drafts SET source_text='modified-test-text' WHERE id=?",(d['draft_id'],))
        finally: release.set()
        response=pending.result(timeout=10)
    assert response.status_code==409
    assert OLD_TEXT not in response.get_data(as_text=True) and OLD_TEXT not in caplog.text
    assert 'fix_simulation_marker' not in response.get_data(as_text=True)+caplog.text
    assert db.execute('SELECT count(*) FROM extraction_runs').fetchone()[0]==0


def test_multiple_inflight_results_cannot_restore_reset_data(app,db,monkeypatch):
    pairs=[prepare(app) for _ in range(3)]
    barrier=threading.Barrier(4); release=threading.Event()
    def model(*args):
        assert not get_db().in_transaction
        barrier.wait(timeout=10); assert release.wait(10); return wire()
    monkeypatch.setattr(ai,'transport',model)
    service=Browser(app)
    with ThreadPoolExecutor(max_workers=3) as pool:
        futures=[pool.submit(b.post,'/parse/extract',d) for b,d in pairs]
        try:
            barrier.wait(timeout=10)
            reset(service) # Completes while all three model calls remain blocked.
        finally: release.set()
        assert [f.result(timeout=10).status_code for f in futures]==[409,409,409]
    for table in ['parse_drafts','parse_previews','extraction_runs']:
        assert db.execute('SELECT count(*) FROM '+table).fetchone()[0]==0
    assert db.execute('SELECT count(*) FROM tickets').fetchone()[0]==3
    assert db.execute('PRAGMA foreign_key_check').fetchall()==[]


@pytest.mark.parametrize('provider',['ollama','rules'])
def test_shared_noncloud_completion_guard(app,db,monkeypatch,provider):
    b,d=prepare(app); app.config['AI_PROVIDER']=provider
    original_parse=ai.parse; entered=threading.Event(); release=threading.Event()
    def local_parse(config,text,*args):
        assert not get_db().in_transaction
        entered.set(); assert release.wait(10)
        return original_parse({**config,'AI_PROVIDER':'rules'},text)
    monkeypatch.setattr(ai,'parse',local_parse)
    service=Browser(app)
    with ThreadPoolExecutor(max_workers=1) as pool:
        pending=pool.submit(b.post,'/parse/extract',d)
        try: assert entered.wait(10); reset(service)
        finally: release.set()
        assert pending.result(timeout=10).status_code==409
    assert db.execute('SELECT count(*) FROM extraction_runs').fetchone()[0]==0


def test_check_and_insert_hold_one_writer_transaction(app,db,monkeypatch):
    b,d=prepare(app); at_insert=threading.Event(); release=threading.Event()
    def model(*args):
        connection=get_db()
        assert not connection.in_transaction
        def trace(statement):
            if statement.lstrip().startswith('INSERT INTO extraction_runs'):
                assert connection.in_transaction
                at_insert.set(); assert release.wait(10)
        connection.set_trace_callback(trace)
        return wire()
    monkeypatch.setattr(ai,'transport',model)
    service=Browser(app); service.role(2)
    html=service.client.get('/demo/reset-data').get_data(as_text=True)
    nonce=re.search(r'name="reset_token" value="([^"]+)"',html).group(1)
    with ThreadPoolExecutor(max_workers=2) as pool:
        pending=pool.submit(b.post,'/parse/extract',d)
        assert at_insert.wait(10)
        reset_pending=pool.submit(service.post,'/demo/reset-data',{'reset_token':nonce,'confirmation':'重置演示数据'})
        try:
            with pytest.raises(TimeoutError): reset_pending.result(timeout=.2)
        finally: release.set()
        assert pending.result(timeout=10).status_code==200
        assert reset_pending.result(timeout=10).status_code==200
    # Linear order: result commits first, reset then removes it; no check/insert gap.
    assert db.execute('SELECT count(*) FROM extraction_runs').fetchone()[0]==0


def test_result_insert_failure_rolls_back(app,db,monkeypatch):
    b,d=prepare(app); monkeypatch.setattr(ai,'transport',lambda *args:wire())
    db.execute("CREATE TRIGGER audit_fail_insert BEFORE INSERT ON extraction_runs BEGIN SELECT RAISE(ABORT,'simulated insert failure'); END")
    assert b.post('/parse/extract',d).status_code==500
    assert db.execute('SELECT count(*) FROM extraction_runs').fetchone()[0]==0
    db.execute('DROP TRIGGER audit_fail_insert')
    fresh=b.post('/parse',{'source_text':'新演示草稿'}).json
    assert b.post('/parse/confirm',{**fresh,'acknowledge':'yes'}).json['confirmed']
    assert b.post('/parse/extract',fresh).status_code==200
