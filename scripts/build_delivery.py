"""Explicit allowlist package, content and configured-key checks, no runtime data."""
import hashlib,json,os,re,subprocess,zipfile
from pathlib import Path
root=Path.cwd()
paths=subprocess.check_output(['git','ls-files','--cached','--others','--exclude-standard','-z']).decode('utf-8').split('\0')
allowed_roots={'park_service','tests','scripts'}
allowed_files={'README.md','requirements.txt','pytest.ini','run.py','start.ps1','start.cmd','.gitignore','.env.example'}
allowed_docs={'implementation_plan.md','business_rules.md','acceptance_matrix.md','stage1_report.md','stage2_report.md','stage3_report.md','demo_script.md'}
selected=[]
for name in set(paths):
    p=Path(name)
    if not p.is_file() or p.name=='stage3-package.json':continue
    if name in allowed_files or (p.parts[0] in allowed_roots and p.suffix in ('.py','.sql','.css','.html','.js','.png')) or (p.parent==Path('docs') and p.name in allowed_docs) or (p.parent==Path('docs/evidence') and p.suffix in ('.json','.xml','.png')):
        selected.append(p)
secret=os.environ.get('DEEPSEEK_API_KEY','')
if not secret and Path('.env').is_file():
    for line in Path('.env').read_text(encoding='utf-8-sig').splitlines():
        k,sep,v=line.partition('=')
        if sep and k.strip()=='DEEPSEEK_API_KEY':secret=v.strip().strip('\"\'')
def inspect(name,data):
    assert not re.search(rb'\bsk-[A-Za-z0-9_-]{20,}',data),'Credential pattern detected; no contents printed'
    assert not (secret and secret.encode('utf-8') in data),'Configured key detected; no contents printed'
    assert not name.startswith(('instance/','.venv/','.git/','.idea/'))
    assert Path(name).name!='.env'
out=Path('dist/park-service-stage3.zip');out.parent.mkdir(exist_ok=True)
with zipfile.ZipFile(out,'w',zipfile.ZIP_DEFLATED) as z:
    for p in sorted(selected):
        data=p.read_bytes();inspect(p.as_posix(),data);z.writestr(p.as_posix(),data)
with zipfile.ZipFile(out) as z:
    assert z.testzip() is None
    for item in z.infolist():inspect(item.filename,z.read(item))
result=dict(archive=out.as_posix(),files=len(selected),sha256=hashlib.sha256(out.read_bytes()).hexdigest(),
    forbidden_entries=0,credential_hits=0,configured_key_comparison=bool(secret),
    note='Allowlist excludes raw materials/PDF, runtime DB/uploads/session/.env/venv/Git. Pattern and exact configured-key checks do not prove absence of every secret.')
Path('docs/evidence/stage3-package.json').write_text(json.dumps(result,indent=2),encoding='utf-8')
print(json.dumps(result))
