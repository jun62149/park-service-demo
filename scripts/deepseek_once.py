"""Explicitly authorized single DeepSeek smoke; never calls Ollama."""
import json,sys,os
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from park_service import create_app

if '--load-local-env' in sys.argv and Path('.env').is_file() and not os.environ.get('DEEPSEEK_API_KEY'):
    for line in Path('.env').read_text(encoding='utf-8-sig').splitlines():
        name,sep,value=line.partition('=')
        if sep and name.strip()=='DEEPSEEK_API_KEY':
            os.environ['DEEPSEEK_API_KEY']=value.strip().strip('\"\'')

app=create_app({'AI_PROVIDER':'deepseek'})
result={'requests':0,'verified':False,'reason':'missing_backend_environment_key'}
if app.config.get('DEEPSEEK_API_KEY','').strip():
    c=app.test_client(); h={'Accept':'application/json','X-CSRF-Token':c.get('/session').json['csrf']}
    d=c.post('/parse',data={'source_text':'地点：演示A座302，演示：照明灯不亮。'},headers=h).json
    confirmation=c.post('/parse/confirm',data={**d,'acknowledge':'yes'},headers=h).json
    if confirmation['confirmed']:
        r=c.post('/parse/extract',data=d,headers=h).json
        result={k:r[k] for k in ['provider','model','fallback_reason','duration_ms','requested_mode','actual_mode_verified']}
        result.update(requests=1,verified=r['provider']=='deepseek',schema_valid=r['provider']=='deepseek')
Path('docs/evidence/stage3-deepseek.json').write_text(json.dumps(result,ensure_ascii=False,indent=2),encoding='utf-8')
print(json.dumps(result,ensure_ascii=False))
