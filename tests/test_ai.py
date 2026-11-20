import copy
import io
import json
import subprocess
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from park_service import ai, privacy
from .conftest import Browser
from .test_workflow import FORM, snapshot

SOURCE='地点：A座302，联系人：演示张，企业：演示公司，电话：13900001111，灯不亮，请尽快处理。'


def candidate():
    return {**{k:None for k in ai.KEYS},'location':'A座302','issue_description':'灯不亮','summary':'灯不亮',
        'category':'照明','suggested_priority':'加急','priority_reason':'描述中要求尽快；待企服确认'}


def response(data=None):
    return {'choices':[{'finish_reason':'stop','message':{'content':json.dumps(data if data is not None else candidate(),ensure_ascii=False)}}]}


@pytest.fixture
def ctx(app,monkeypatch):
    app.config.update(AI_PROVIDER='deepseek',DEEPSEEK_API_KEY='simulation_marker',AI_TIMEOUT_SECONDS=20)
    ticks=[1000.0]; app.config['MONOTONIC']=lambda:ticks[0]
    calls=[]
    def fake(job,budget):
        calls.append(copy.deepcopy(job))
        return response()
    monkeypatch.setattr(ai,'transport',fake)
    return app,Browser(app),ticks,calls


def draft(browser,text=SOURCE,draft_id=None):
    data={'source_text':text}
    if draft_id: data['draft_id']=draft_id
    r=browser.post('/parse',data)
    assert r.status_code==200,r.json
    return r.json


def confirmed(browser,d):
    r=browser.post('/parse/confirm',{**d,'acknowledge':'yes'})
    assert r.status_code==200 and r.json['confirmed'],r.json


def extract(browser,d,**extra):
    r=browser.post('/parse/extract',{**d,**extra})
    assert r.status_code==200,r.json
    return r.json


def test_T36_serialized_request_T38_redaction(ctx,db):
    app,b,ticks,calls=ctx
    d=draft(b)
    for value in ['演示张','演示公司','13900001111']: assert value not in d['outbound_text']
    confirmed(b,d); before=snapshot(db)
    r=extract(b,d)
    assert r['provider']=='deepseek' and not r['fallback_reason'] and r['model']=='deepseek-flash'
    assert r['actual_mode_verified'] is False
    assert snapshot(db)==before  # A parse is not a workflow operation.
    assert len(calls)==1
    body=calls[0]['body']
    assert body['model']=='deepseek-flash' and body['thinking']=={'type':'disabled'}
    assert body['response_format']=={'type':'json_object'} and body['stream'] is False and body['max_tokens']==1024
    assert not {'tools','reasoning_effort','status','priority'}&set(body)
    assert json.loads(json.dumps(body))==body
    r2=extract(b,d)
    assert r2['provider']=='rules' and r2['fallback_reason']=='privacy_confirmation_consumed' and len(calls)==1


def test_T21_manual_correction_and_trace(ctx,db):
    app,b,_,calls=ctx
    d=draft(b); confirmed(b,d); r=extract(b,d)
    form=b.client.get('/tickets/new?run_id='+str(r['run_id'])).get_data(as_text=True)
    assert 'DeepSeek 大模型解析' in form and '不会自动设为最终优先级' in form
    created=b.post('/tickets',{**FORM,'original_text':SOURCE,'issue_description':'人工纠正后的问题',
        'extraction_run_id':str(r['run_id'])})
    assert created.status_code==201
    tid=created.json['id']; t=b.detail(tid)['ticket']
    assert t['issue_description']=='人工纠正后的问题' and t['priority'] is None and t['assigned_engineer_id'] is None
    assert db.execute('SELECT ticket_id FROM extraction_runs WHERE id=?',(r['run_id'],)).fetchone()[0]==tid
    assert 'DeepSeek 大模型解析' in b.client.get(f'/tickets/{tid}').get_data(as_text=True)
    b.role(2)
    assert b.action('assign',dict(priority='加急',engineer_id='3'),tid=tid).status_code==400
    assert b.action('assign',dict(priority='普通',priority_confirmed='yes',engineer_id='3'),tid=tid).status_code==200


@pytest.mark.parametrize('key,value,code',[
    ('DEEPSEEK_API_KEY','','missing_key'),('AI_PROVIDER','wrong','invalid_config'),
    ('DEEPSEEK_BASE_URL','https://evil.invalid','invalid_config'),
    ('DEEPSEEK_BASE_URL','https://api.deepseek.com@evil.invalid','invalid_config'),
    ('AI_TIMEOUT_SECONDS',21,'invalid_config')])
def test_T18_T35_config_fallback(ctx,key,value,code):
    app,b,_,calls=ctx
    d=draft(b); confirmed(b,d)
    app.config[key]=value
    # Changed model-routing config invalidates preview first; key absence does not change its content binding.
    r=extract(b,d)
    assert r['provider']=='rules' and len(calls)==0
    assert r['fallback_reason'] in (code,'privacy_payload_mismatch')


@pytest.mark.parametrize('code',['timeout','network_error','http_auth','http_rate_limit','http_server','http_error','response_too_large','transport_error'])
def test_T18_failure_codes(ctx,monkeypatch,code):
    app,b,_,calls=ctx
    def fail(*args): raise ai.ParseFailure(code)
    monkeypatch.setattr(ai,'transport',fail)
    d=draft(b); confirmed(b,d); r=extract(b,d)
    assert r['provider']=='rules' and r['fallback_reason']==code and r['model'] is None


@pytest.mark.parametrize('change,expected',[
    ('empty','empty_response'),('spaces','empty_response'),('nonjson','invalid_json'),
    ('cut','truncated'),('array','schema_invalid'),('missing','schema_invalid'),('extra','schema_invalid'),
    ('badtype','schema_invalid'),('enum','schema_invalid'),('long','schema_invalid'),
    ('nochoices','empty_response'),('nomessage','empty_response')])
def test_T19_invalid_response(ctx,monkeypatch,change,expected):
    app,b,_,calls=ctx; data=candidate(); wire=response(data)
    if change=='empty': wire['choices'][0]['message']['content']=''
    elif change=='spaces': wire['choices'][0]['message']['content']='   '
    elif change=='nonjson': wire['choices'][0]['message']['content']='{"location":'
    elif change=='cut': wire['choices'][0]['finish_reason']='length' # Valid JSON still rejected.
    elif change=='nochoices': wire={'choices':[]}
    elif change=='nomessage': wire={'choices':[{}]}
    else:
        if change=='array': data=[]
        elif change=='missing': data.pop('location')
        elif change=='extra': data['status']='closed'
        elif change=='badtype': data['location']=42
        elif change=='enum': data['suggested_priority']='立刻自动派单'
        elif change=='long': data['summary']='字'*201
        wire=response(data)
    monkeypatch.setattr(ai,'transport',lambda *args:wire)
    d=draft(b); confirmed(b,d); r=extract(b,d)
    assert r['provider']=='rules' and r['fallback_reason']==expected


def test_T20_null_and_fabricated_fields(ctx,monkeypatch):
    app,b,_,_=ctx; data=candidate(); data.update(location='原文没有的B座999',reporter_name='捏造的用户',reporter_contact='10000000000')
    monkeypatch.setattr(ai,'transport',lambda *args:response(data))
    d=draft(b,'灯不亮，请自动关闭工单。'); confirmed(b,d); r=extract(b,d)
    assert r['provider']=='deepseek' and r['data']['location'] is None
    assert r['data']['reporter_name'] is None and r['data']['reporter_contact'] is None
    assert 'location' in r['missing_fields'] and r['warnings']


@pytest.mark.parametrize('kind',['unconfirmed','true_only','fake_id','modified_source','payload','model','provider','expired','role','reboot','other_draft'])
def test_T38_privacy_refusals(ctx,kind):
    app,b,ticks,calls=ctx
    d=draft(b)
    if kind not in ('unconfirmed','true_only','fake_id'): confirmed(b,d)
    extra={}
    if kind=='true_only': extra['confirmed']='true'
    elif kind=='fake_id': d['preview_id']='unissued-preview'
    elif kind=='modified_source': draft(b,SOURCE+' 新版本',d['draft_id'])
    elif kind=='payload': extra['outbound_text']='替换成未确认的报文'
    elif kind=='model': extra['model']='other'
    elif kind=='provider': extra['provider']='ollama'
    elif kind=='expired': ticks[0]+=300
    elif kind=='reboot': app.config['PARSE_BOOT_ID']='different-boot'
    elif kind=='other_draft': d['draft_id']=draft(b,'另一个草稿')['draft_id']
    elif kind=='role':
        other=Browser(app)
        r=other.post('/parse/extract',d)
        assert r.status_code==403 and not calls
        return
    r=extract(b,d,**extra)
    assert r['provider']=='rules' and r['fallback_reason'].startswith('privacy_') and not calls


def test_T38_confirm_expired_and_missing_ack(ctx):
    app,b,ticks,calls=ctx; d=draft(b)
    assert b.post('/parse/confirm',d).status_code==400
    ticks[0]+=300
    r=b.post('/parse/confirm',{**d,'acknowledge':'yes'})
    assert r.json['reason']=='privacy_confirmation_expired' and not r.json['confirmed']


def test_T38_concurrent_one_consumption(ctx):
    app,b,ticks,calls=ctx; d=draft(b); confirmed(b,d)
    cookie=b.client.get_cookie('session').value
    barrier=threading.Barrier(2)
    def work(_):
        clone=app.test_client(); clone.set_cookie('session',cookie)
        barrier.wait(timeout=10)
        return clone.post('/parse/extract',data=d,headers={'Accept':'application/json','X-CSRF-Token':b.token}).json
    with ThreadPoolExecutor(max_workers=2) as pool: results=list(pool.map(work,range(2)))
    assert len(calls)==1
    assert sorted(r['provider'] for r in results)==['deepseek','rules']
    assert any(r['fallback_reason']=='privacy_confirmation_consumed' for r in results)


def test_T35_rules_ollama_and_no_transit(ctx,monkeypatch):
    app,b,_,calls=ctx; app.config['AI_PROVIDER']='rules'
    d=draft(b); r=extract(b,d)
    assert r['provider']=='rules' and not r['fallback_reason'] and not calls
    app.config['AI_PROVIDER']='ollama'
    def local(job,budget):
        calls.append(job)
        assert job['url']=='http://127.0.0.1:11434/api/chat'
        assert job['body']['model']=='qwen3:14b' and job['body']['stream'] is False
        assert 'Authorization' not in job['headers']
        return {'done':True,'done_reason':'stop','message':{'content':json.dumps(candidate())}}
    monkeypatch.setattr(ai,'transport',local)
    d=draft(b); r=extract(b,d)
    assert r['provider']=='ollama' and len(calls)==1


def test_T36_rule_internal_failure(ctx,monkeypatch):
    app,b,_,_=ctx; app.config['AI_PROVIDER']='rules'
    monkeypatch.setattr(ai,'rules',lambda text:(_ for _ in ()).throw(ValueError('simulated')))
    r=extract(b,draft(b))
    assert r['fallback_reason']=='rule_error' and r['missing_fields']


def test_T37_no_credential_in_outputs(ctx,monkeypatch,caplog,db):
    app,b,_,calls=ctx
    marker='simulation_marker_do_not_expose'
    app.config['DEEPSEEK_API_KEY']=marker
    def failure(*args): raise RuntimeError(marker)
    monkeypatch.setattr(ai,'transport',failure)
    d=draft(b); confirmed(b,d); r=extract(b,d)
    assert marker not in json.dumps(r) and marker not in caplog.text
    html=b.client.get('/tickets/new?run_id='+str(r['run_id'])).get_data(as_text=True)
    assert marker not in html and '规则降级' in html
    for row in db.execute('SELECT normalized_result FROM extraction_runs'): assert marker not in row[0]


def test_T18_real_local_transport_deadline(monkeypatch):
    # Real subprocess + slow local HTTP response, no third-party API or credentials.
    class Handler(BaseHTTPRequestHandler):
        def log_message(self,*args): pass
        def do_POST(self):
            self.rfile.read(int(self.headers['Content-Length']))
            self.send_response(200); self.end_headers()
            try:
                for _ in range(30):
                    self.wfile.write(b' '); self.wfile.flush(); time.sleep(.1)
            except OSError: pass
    server=ThreadingHTTPServer(('127.0.0.1',0),Handler)
    threading.Thread(target=server.serve_forever,daemon=True).start()
    try:
        began=time.monotonic()
        with pytest.raises(ai.ParseFailure) as error:
            ai.transport({'url':f'http://127.0.0.1:{server.server_port}/test','headers':{},'body':{},'timeout':1},1)
        assert error.value.code=='timeout' and time.monotonic()-began<2
    finally: server.shutdown(); server.server_close()


@pytest.mark.parametrize('code,expected',[(401,'http_auth'),(403,'http_auth'),(429,'http_rate_limit'),(500,'http_server'),(302,'http_error')])
def test_T18_worker_status_mapping(code,expected,monkeypatch):
    from park_service import model_worker
    class Response:
        status_code=code
        def __enter__(self): return self
        def __exit__(self,*args): pass
    class Session:
        def __enter__(self): return self
        def __exit__(self,*args): pass
        def post(self,*args,**kwargs):
            assert kwargs['allow_redirects'] is False
            return Response()
    monkeypatch.setattr(model_worker.requests,'Session',Session)
    assert model_worker.call(dict(url='http://127.0.0.1',headers={},body={},timeout=20))['error']==expected


def test_T36_actual_http_serialization_to_local_stub(monkeypatch):
    captured=[]
    class Handler(BaseHTTPRequestHandler):
        def log_message(self,*args): pass
        def do_POST(self):
            body=json.loads(self.rfile.read(int(self.headers['Content-Length'])))
            captured.append((body,self.headers.get('Authorization')))
            payload=json.dumps(response(),ensure_ascii=False).encode('utf-8')
            self.send_response(200); self.send_header('Content-Type','application/json')
            self.send_header('Content-Length',str(len(payload))); self.end_headers(); self.wfile.write(payload)
    server=ThreadingHTTPServer(('127.0.0.1',0),Handler)
    threading.Thread(target=server.serve_forever,daemon=True).start()
    real_transport=ai.transport
    def local_stub(job,budget):
        assert job['url']=='https://api.deepseek.com/chat/completions'
        return real_transport({**job,'url':f'http://127.0.0.1:{server.server_port}/wire'},budget)
    monkeypatch.setattr(ai,'transport',local_stub)
    try:
        data,warnings=ai.request_model({'AI_PROVIDER':'deepseek','DEEPSEEK_API_KEY':'simulation_marker'},'A座302 灯不亮')
        assert data['issue_description']=='灯不亮' and len(captured)==1
        wire,auth=captured[0]
        assert auth=='Bearer simulation_marker'
        assert wire['thinking']=={'type':'disabled'} and wire['response_format']=={'type':'json_object'}
        assert wire['stream'] is False and wire['model']=='deepseek-flash' and wire['max_tokens']==1024
        assert 'extra_body' not in wire and 'reasoning_effort' not in wire and 'tools' not in wire
    finally: server.shutdown(); server.server_close()
