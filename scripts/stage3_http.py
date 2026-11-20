import json,re
from pathlib import Path
import requests
s=requests.Session(); base='http://127.0.0.1:5053'; checks=[]
def get(path):
    r=s.get(base+path,headers={'Accept':'application/json'},timeout=5); assert r.status_code==200
    checks.append((path,r.status_code)); return r
def post(path,data,expected=200):
    token=get('/session').json()['csrf']
    r=s.post(base+path,data=data,headers={'Accept':'application/json','X-CSRF-Token':token},timeout=5)
    assert r.status_code==expected,(path,r.status_code);checks.append((path,r.status_code));return r
post('/role',{'user_id':'2'})
before=get('/workspace').json()['metrics'];assert sum(before['states'].values())==before['total']
post('/demo/advance',{'seconds':'300'})
page=get('/demo/reset-data').text
nonce=re.search(r'name="reset_token" value="([^"]+)"',page).group(1)
post('/demo/reset-data',{'reset_token':nonce,'confirmation':'bad'},400)
post('/demo/reset-data',{'reset_token':nonce,'confirmation':'重置演示数据'})
assert get('/reminders').json()['offset_seconds']==0
post('/role',{'user_id':'5'})
after=get('/workspace').json()['metrics']; assert after['total']==3 and after['states']['pending_assignment']==3 and after['close_rate']==0
post('/demo/reset-data',{'reset_token':nonce,'confirmation':'重置演示数据'},403)
result=dict(result='passed',checks=checks,before=before,after=after,clock_reset=True)
Path('docs/evidence/stage3-http.json').write_text(json.dumps(result,ensure_ascii=False,indent=2),encoding='utf-8')
print(json.dumps(result,ensure_ascii=False))
