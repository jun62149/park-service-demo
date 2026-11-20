"""Pattern-based credential audit; never prints matched contents or environment."""
import io
import json
import re
import subprocess
import zipfile
import argparse
from pathlib import Path

PATTERNS=[rb'\bsk-[A-Za-z0-9_-]{20,}',rb'(?i)Bearer\s+[A-Za-z0-9_-]{25,}',
          rb'\bou_[a-f0-9]{32}\b', rb'\b[atu]-[A-Za-z0-9_-]{32,}\b']


def hit(data): return any(re.search(pattern,data) for pattern in PATTERNS)


def main():
    parser=argparse.ArgumentParser()
    parser.add_argument('--output',default='docs/evidence/stage2-security-audit.json')
    args=parser.parse_args()
    candidates=subprocess.check_output(['git','ls-files','--cached','--others','--exclude-standard','-z']).decode('utf-8').split('\0')
    paths={p for p in candidates if p}
    # Include project log/archive artifacts even though ignored; never read .env or session files.
    for child in Path('.').iterdir():
        if child.name in ('.git','.venv','instance','.idea','.pytest_cache'): continue
        if child.is_file() and child.suffix in ('.log','.zip'): paths.add(child.as_posix())
        elif child.is_dir():
            paths.update(p.as_posix() for p in child.rglob('*') if p.is_file() and p.suffix in ('.log','.zip'))
    findings=[]; archives=0; logs=0
    for name in sorted(paths):
        p=Path(name)
        if not p.is_file(): continue
        data=p.read_bytes()
        if hit(data): findings.append({'scope':'working_tree','path':name})
        logs+=p.suffix=='.log'
        if p.suffix=='.zip':
            archives+=1
            with zipfile.ZipFile(io.BytesIO(data)) as archive:
                for entry in archive.infolist():
                    if entry.file_size>20_000_000: raise SystemExit('Archive entry too large to audit safely')
                    if hit(archive.read(entry)): findings.append({'scope':'zip_entry','path':name})
    objects=subprocess.check_output(['git','rev-list','--objects','--all']).splitlines()
    blobs=0
    for record in objects:
        oid=record.split(b' ',1)[0].decode('ascii')
        if subprocess.check_output(['git','cat-file','-t',oid]).strip()!=b'blob': continue
        blobs+=1
        if hit(subprocess.check_output(['git','cat-file','blob',oid])):
            findings.append({'scope':'reachable_history','object':oid})
    forbidden=[]
    for name in subprocess.check_output(['git','ls-files','-z']).decode('utf-8').split('\0'):
        p=Path(name)
        if name and (name.startswith(('instance/','.venv/')) or (p.name.startswith('.env') and p.name!='.env.example') or p.suffix in ('.db','.sqlite3','.log','.zip')):
            forbidden.append(name)
    example=Path('.env.example').read_text(encoding='utf-8')
    assert re.search(r'^DEEPSEEK_API_KEY=$',example,re.M)
    assert re.search(r'^FEISHU_APP_SECRET=$',example,re.M)
    result=dict(method='High-confidence credential patterns, raw reachable Git blobs, tracked-path policy; no matched contents printed.',
        working_files=len(paths),reachable_blobs=blobs,log_files=logs,zip_archives=archives,
        findings=findings,forbidden_tracked_paths=forbidden,
        limits='Pattern audit is not proof of no secrets. No runtime DB/session/.env read. ZIP/log coverage is given by the counters; absent artifacts are not marked verified.')
    Path(args.output).write_text(json.dumps(result,ensure_ascii=False,indent=2),encoding='utf-8')
    print(json.dumps(result,ensure_ascii=False))
    if findings or forbidden: raise SystemExit(1)


if __name__=='__main__':main()
