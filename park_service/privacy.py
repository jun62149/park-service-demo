import hashlib
import json
import secrets
import time

from flask import current_app
from werkzeug.exceptions import BadRequest, Conflict, Forbidden, NotFound

from . import ai
from .clock import iso_now


def digest(value): return hashlib.sha256(value.encode('utf-8')).hexdigest()


def mono(): return current_app.config.get('MONOTONIC',time.monotonic)()


def fingerprint():
    return digest(json.dumps({**ai.settings(current_app.config),'schema':ai.SCHEMA_VERSION,'policy':'redact_v1'},sort_keys=True))


def get_draft(db,draft_id,owner,actor):
    if not isinstance(draft_id,str) or len(draft_id)>100: raise NotFound('解析草稿不存在')
    draft=db.execute('SELECT * FROM parse_drafts WHERE id=?',(draft_id,)).fetchone()
    if not draft: raise NotFound('解析草稿不存在')
    if draft['owner_session']!=owner or draft['actor_id']!=actor['id']: raise Forbidden('无权访问该解析草稿')
    return dict(draft)


def save_draft(db,owner,actor,text,draft_id=None):
    if actor['role'] not in ('reporter','service'): raise Forbidden('当前身份不能创建解析草稿')
    if not isinstance(text,str) or not 1<=len(text.strip())<=2000: raise BadRequest('报修原文应为 1–2000 字')
    text=text.strip()
    db.execute('BEGIN IMMEDIATE')
    try:
        if draft_id:
            draft=get_draft(db,draft_id,owner,actor)
            db.execute('UPDATE parse_drafts SET source_text=?,version=version+1 WHERE id=?',(text,draft_id))
        else:
            draft_id=secrets.token_urlsafe(24)
            db.execute('INSERT INTO parse_drafts VALUES(?,?,?,?,?)',(draft_id,owner,actor['id'],1,text))
        db.commit()
    except Exception:
        db.rollback(); raise
    return get_draft(db,draft_id,owner,actor)


def make_preview(db,draft):
    preview_id=secrets.token_urlsafe(32)
    outbound=ai.redact(draft['source_text'])
    db.execute('''INSERT INTO parse_previews(id,draft_id,draft_version,source_hash,outbound_text,outbound_hash,fingerprint,boot_id,created_monotonic)
        VALUES(?,?,?,?,?,?,?,?,?)''',(preview_id,draft['id'],draft['version'],digest(draft['source_text']),outbound,digest(outbound),
        fingerprint(),current_app.config['PARSE_BOOT_ID'],mono()))
    return preview_id,outbound


def check(db,preview_id,owner,actor,data,confirmed=False):
    if not isinstance(preview_id,str) or len(preview_id)>100: return None,None,'privacy_not_confirmed'
    row=db.execute('SELECT * FROM parse_previews WHERE id=?',(preview_id,)).fetchone()
    if not row: return None,None,'privacy_not_confirmed'
    draft=get_draft(db,row['draft_id'],owner,actor)
    if row['boot_id']!=current_app.config['PARSE_BOOT_ID'] or mono()-row['created_monotonic']>=300:
        return row,draft,'privacy_confirmation_expired'
    if row['consumed_monotonic'] is not None: return row,draft,'privacy_confirmation_consumed'
    mismatch=(row['draft_version']!=draft['version'] or row['source_hash']!=digest(draft['source_text']) or
        row['outbound_hash']!=digest(row['outbound_text']) or row['fingerprint']!=fingerprint())
    for key,expected in [('source_text',draft['source_text']),('outbound_text',row['outbound_text']),
                         ('provider',ai.settings(current_app.config)['provider']),('model',ai.settings(current_app.config)['model'])]:
        if key in data and data[key]!=expected: mismatch=True
    if mismatch: return row,draft,'privacy_payload_mismatch'
    if confirmed and row['confirmed_monotonic'] is None: return row,draft,'privacy_not_confirmed'
    return row,draft,None


def confirm(db,preview_id,owner,actor,data):
    db.execute('BEGIN IMMEDIATE')
    try:
        row,draft,reason=check(db,preview_id,owner,actor,data)
        if reason is None:
            db.execute('UPDATE parse_previews SET confirmed_monotonic=? WHERE id=?',(mono(),preview_id))
        db.commit()
    except Exception:
        db.rollback(); raise
    return reason


def consume(db,draft_id,preview_id,owner,actor,data):
    db.execute('BEGIN IMMEDIATE')
    try:
        draft=get_draft(db,draft_id,owner,actor)
        try:
            row,bound,reason=check(db,preview_id,owner,actor,data,confirmed=True)
        except (Forbidden,NotFound):
            row,bound,reason=None,None,'privacy_not_confirmed'
        if bound and bound['id']!=draft_id: reason='privacy_payload_mismatch'
        if reason is None:
            db.execute('UPDATE parse_previews SET consumed_monotonic=? WHERE id=?',(mono(),preview_id))
        db.commit()
        return draft,row['outbound_text'] if reason is None else None,reason
    except Exception:
        db.rollback(); raise


def extract(db,draft_id,preview_id,owner,actor,data):
    # Bind one consistent generation/draft snapshot, without holding a write
    # transaction across any model or rule-provider work.
    db.execute('BEGIN IMMEDIATE')
    try:
        generation=db.execute('SELECT generation FROM demo_meta WHERE id=1').fetchone()[0]
        draft=get_draft(db,draft_id,owner,actor)
        db.commit()
    except NotFound:
        db.rollback()
        raise Conflict('解析草稿已失效，请重新创建并确认预览') from None
    except Exception:
        db.rollback(); raise
    cfg=ai.settings(current_app.config)
    outbound=None; reason=None
    if cfg['provider']=='deepseek':
        try:
            consumed_draft,outbound,reason=consume(db,draft_id,preview_id,owner,actor,data)
        except (NotFound,Forbidden):
            raise Conflict('解析草稿已失效，请重新创建并确认预览') from None
        if consumed_draft!=draft:
            raise Conflict('解析草稿已变化，请重新创建并确认预览')
    result=ai.parse(current_app.config,draft['source_text'],outbound,reason)
    # Reset/save_draft use the same SQLite writer lock. Revalidation and INSERT
    # must share this transaction, so reset cannot interleave between them.
    db.execute('BEGIN IMMEDIATE')
    try:
        current_generation=db.execute('SELECT generation FROM demo_meta WHERE id=1').fetchone()[0]
        current_draft=db.execute('SELECT * FROM parse_drafts WHERE id=?',(draft_id,)).fetchone()
        if (current_generation!=generation or current_draft is None or
                current_draft['version']!=draft['version'] or
                current_draft['owner_session']!=owner or current_draft['actor_id']!=actor['id'] or
                current_draft['source_text']!=draft['source_text']):
            raise Conflict('解析期间数据已重置或草稿已变化，请重新创建并确认预览')
        run_id=db.execute('''INSERT INTO extraction_runs(actor_id,owner_session,source_text,requested_provider,provider,model,
            duration_ms,fallback_reason,schema_version,normalized_result,created_at,redaction_applied) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)''',
            (actor['id'],owner,draft['source_text'],result['requested_provider'],result['provider'],result['model'],result['duration_ms'],
             result['fallback_reason'],ai.SCHEMA_VERSION,json.dumps(result,ensure_ascii=False),iso_now(),int(outbound is not None))).lastrowid
        db.commit()
    except Exception:
        db.rollback(); raise
    return run_id,result,draft
