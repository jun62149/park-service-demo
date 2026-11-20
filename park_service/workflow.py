import hashlib
import json
import uuid
from datetime import datetime, timezone, timedelta

from werkzeug.exceptions import BadRequest, Conflict, Forbidden, NotFound

from . import files as evidence
from .clock import iso_now, real_now

STATES = {'pending_assignment': '待分派', 'pending_acceptance': '待接单',
          'in_progress': '处理中', 'pending_service_review': '待企服验收',
          'returned': '已退回', 'closed': '已关闭'}
PRIORITIES = ('普通', '加急', '特急')
ACTIONS = {'create': '创建', 'edit': '修改待分派信息', 'assign': '分派（站内模拟推送）',
           'reassign': '改派（站内模拟推送）', 'update_assignment': '更新派单信息',
           'accept': '工程接单', 'submit_completion': '提交完工', 'reject': '企服退回',
           'resume': '重新处理', 'close': '企服验收关闭'}


def now():
    return iso_now()


def text_field(data, key, limit, required=True):
    value = data.get(key, '')
    if not isinstance(value, str):
        raise BadRequest(f'{key} 必须为文本')
    value = value.strip()
    if (required and not value) or len(value) > limit:
        labels = {'reporter_name': '报修人', 'reporter_contact': '联系途径', 'location': '具体地点',
                  'original_text': '原始报修描述', 'issue_description': '问题描述',
                  'resolution_note': '处理说明', 'reason': '原因'}
        raise BadRequest(f'{labels.get(key,key)}不能为空且最多 {limit} 字' if required else f'{key} 最多 {limit} 字')
    return value


def fields(data, original=True):
    result = {k: text_field(data, k, n) for k, n in
              [('reporter_name', 50), ('reporter_contact', 100), ('location', 200), ('issue_description', 2000)]}
    for k, n in [('company_name', 100), ('summary', 200), ('category', 50)]:
        result[k] = text_field(data, k, n, False)
    if original:
        result['original_text'] = text_field(data, 'original_text', 2000)
    return result


def get_ticket(db, ticket_id):
    row = db.execute('SELECT * FROM tickets WHERE id=?', (ticket_id,)).fetchone()
    if row is None:
        raise NotFound('工单不存在')
    return dict(row)


def can_view(actor, ticket):
    return (actor['role'] in ('service', 'manager') or
            (actor['role'] == 'reporter' and ticket['reporter_id'] == actor['id']) or
            (actor['role'] == 'engineer' and ticket['assigned_engineer_id'] == actor['id']))


def require_view(actor, ticket):
    if not can_view(actor, ticket):
        raise Forbidden('当前演示身份无权访问该工单或凭证')


def event(db, actor, before, after, action, reason='', submission_id=None, request_id=None):
    cursor = db.execute('''INSERT INTO ticket_events(ticket_id,ticket_version,actor_id,actor_role,actor_name,
       action,from_status,to_status,before_values,after_values,effective_at,real_recorded_at,
       reason,assignment_id,submission_id,request_id) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)''',
       (after['id'], after['version'], actor['id'], actor['role'], actor['display_name'], action,
        before['status'] if before else None, after['status'],
        json.dumps(before or {}, ensure_ascii=False), json.dumps(after, ensure_ascii=False),
        after['updated_at'], real_now().isoformat(), reason, after['current_assignment_id'], submission_id, request_id or uuid.uuid4().hex))
    from .notifications import enqueue_event
    enqueue_event(db, cursor.lastrowid, after, action)


def create_ticket(db, actor, data, in_transaction=False, request_id=None, extraction_run_id=None, seed_id=None):
    if actor['role'] not in ('reporter', 'service'):
        raise Forbidden('只有报修人或企服可创建工单')
    values = fields(data)
    stamp = now()
    values.update(number='DEMO-' + uuid.uuid4().hex[:16].upper(), reporter_id=actor['id'],
                  status='pending_assignment', created_at=stamp, updated_at=stamp, state_changed_at=stamp)
    if seed_id is not None:
        values['id']=seed_id
    if not in_transaction:
        db.execute('BEGIN IMMEDIATE')
    try:
        if request_id and db.execute('SELECT 1 FROM ticket_events WHERE request_id=?', (request_id,)).fetchone():
            raise Conflict('报修已提交，请勿重复操作；返回列表查看')
        cursor = db.execute(f"INSERT INTO tickets({','.join(values)}) VALUES({','.join('?' for _ in values)})", tuple(values.values()))
        ticket = get_ticket(db, cursor.lastrowid)
        event(db, actor, None, ticket, 'create', request_id=request_id)
        if extraction_run_id is not None:
            row=db.execute('SELECT * FROM extraction_runs WHERE id=? AND actor_id=? AND ticket_id IS NULL', (extraction_run_id,actor['id'])).fetchone()
            if not row or row['source_text']!=values['original_text']:
                raise BadRequest('解析记录与本次工单原文不一致，请重新解析或清除解析引用')
            db.execute('UPDATE extraction_runs SET ticket_id=? WHERE id=?',(ticket['id'],extraction_run_id))
        if not in_transaction:
            db.commit()
        return ticket['id']
    except Exception:
        if not in_transaction:
            db.rollback()
        raise


def engineer_id(db, data):
    try:
        uid = int(data.get('engineer_id', ''))
    except (ValueError, TypeError):
        raise BadRequest('请选择有效的演示工程人员')
    user = db.execute("SELECT * FROM users WHERE id=? AND role='engineer' AND active=1", (uid,)).fetchone()
    if not user:
        raise BadRequest('请选择有效的演示工程人员')
    return uid


def assignment_fields(data):
    priority = data.get('priority')
    if priority not in PRIORITIES or data.get('priority_confirmed') != 'yes':
        raise BadRequest('企服必须显式确认普通、加急、特急之一')
    due = text_field(data, 'due_at', 40, False)
    if due:
        try:
            parsed = datetime.fromisoformat(due)
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=timezone(timedelta(hours=8)))
            due = parsed.astimezone(timezone.utc).isoformat(timespec='microseconds')
        except ValueError:
            raise BadRequest('截止时间格式无效（输入按上海时间解释）')
    return priority, due or None


def apply_action(db, upload_root, actor, ticket_id, action, data, photos=()):
    # Decode before holding SQLite's write lock; ownership/version rechecked inside.
    preliminary = get_ticket(db, ticket_id)
    require_view(actor, preliminary)
    service_actions = {'assign', 'reassign', 'update_assignment', 'edit', 'reject', 'close'}
    engineer_actions = {'accept', 'resume', 'submit_completion'}
    if action not in service_actions | engineer_actions:
        raise BadRequest('未知操作')
    if (action in service_actions and actor['role'] != 'service') or (
            action in engineer_actions and (actor['role'] != 'engineer' or preliminary['assigned_engineer_id'] != actor['id'])):
        raise Forbidden('当前身份不能执行此操作')
    try:
        expected_version = int(data.get('version', ''))
    except (ValueError, TypeError):
        raise BadRequest('缺少有效工单版本，请刷新')
    allowed = {'assign': 'pending_assignment', 'reassign': 'pending_acceptance',
               'update_assignment': 'pending_acceptance', 'edit': 'pending_assignment',
               'accept': 'pending_acceptance', 'submit_completion': 'in_progress',
               'reject': 'pending_service_review', 'resume': 'returned', 'close': 'pending_service_review'}
    if preliminary['version'] != expected_version or preliminary['status'] != allowed[action]:
        raise Conflict('工单版本或状态已变化，请刷新；已关闭工单不能继续修改')
    prepared = []
    note = None
    if action == 'submit_completion':
        if data.get('attachment_id') or data.get('submission_id'):
            raise BadRequest('不能借用其他工单或历史轮次凭证，请上传本轮照片')
        note = text_field(data, 'resolution_note', 1000)
        prepared = evidence.prepare_photos(photos)
    written = []
    db.execute('BEGIN IMMEDIATE')
    try:
        before = get_ticket(db, ticket_id)
        require_view(actor, before)
        if action in engineer_actions and before['assigned_engineer_id'] != actor['id']:
            raise Forbidden('该工单已改派，当前工程无权操作')
        if before['version'] != expected_version or before['status'] != allowed[action]:
            raise Conflict('工单版本或状态已变化，请刷新')
        stamp = now()
        changes = {'updated_at': stamp, 'version': expected_version + 1}
        reason = ''
        submission_id = None
        if action == 'edit':
            if 'original_text' in data and data['original_text'] != before['original_text']:
                raise BadRequest('原始报修文本不可覆盖')
            changes.update(fields(data, original=False))
        elif action in ('assign', 'reassign', 'update_assignment'):
            priority, due = assignment_fields(data)
            changes.update(priority=priority, priority_confirmed_by=actor['id'], priority_confirmed_at=stamp, due_at=due)
            if due != before['due_at']:
                changes['due_version'] = before['due_version'] + 1
            if action != 'assign':
                reason = text_field(data, 'reason', 500)
            if action in ('assign', 'reassign'):
                uid = engineer_id(db, data)
                if action == 'reassign' and uid == before['assigned_engineer_id']:
                    raise BadRequest('改派对象必须与原负责人不同')
                if before['current_assignment_id']:
                    db.execute('UPDATE assignments SET ended_at=? WHERE id=?', (stamp, before['current_assignment_id']))
                seq = db.execute('SELECT coalesce(max(sequence),0)+1 FROM assignments WHERE ticket_id=?', (ticket_id,)).fetchone()[0]
                assignment = db.execute('''INSERT INTO assignments(ticket_id,sequence,engineer_id,assigned_by,assigned_at,pushed_at,reason)
                    VALUES(?,?,?,?,?,?,?)''', (ticket_id, seq, uid, actor['id'], stamp, stamp, reason)).lastrowid
                changes.update(status='pending_acceptance', assigned_engineer_id=uid, current_assignment_id=assignment)
            elif priority == before['priority'] and due == before['due_at']:
                raise BadRequest('优先级与截止时间均未变化')
        elif action in ('accept', 'resume'):
            changes['status'] = 'in_progress'
            if action == 'accept':
                changes['accepted_at'] = stamp
                db.execute('UPDATE assignments SET accepted_at=? WHERE id=?', (stamp, before['current_assignment_id']))
        elif action == 'submit_completion':
            round_no = db.execute('SELECT coalesce(max(round),0)+1 FROM completion_submissions WHERE ticket_id=?', (ticket_id,)).fetchone()[0]
            submission_id = db.execute('''INSERT INTO completion_submissions(ticket_id,round,engineer_id,resolution_note,submitted_at)
                 VALUES(?,?,?,?,?)''', (ticket_id, round_no, actor['id'], note, stamp)).lastrowid
            for photo in prepared:
                path = evidence.safe_path(upload_root, photo['generated_name'])
                written.append(path)
                evidence.write_photo(path, photo['data'])
                db.execute('''INSERT INTO attachments(ticket_id,submission_id,generated_name,original_name,detected_type,size,hash,uploaded_by,uploaded_at)
                    VALUES(?,?,?,?,?,?,?,?,?)''', (ticket_id, submission_id, photo['generated_name'], photo['original_name'],
                    photo['detected_type'], photo['size'], photo['hash'], actor['id'], stamp))
            changes.update(status='pending_service_review', submitted_at=stamp)
        elif action in ('reject', 'close'):
            submission = db.execute('SELECT * FROM completion_submissions WHERE ticket_id=? ORDER BY round DESC LIMIT 1', (ticket_id,)).fetchone()
            if not submission or submission['review_result'] is not None or not submission['resolution_note'].strip():
                raise Conflict('当前完工轮次不可验收')
            attachments = db.execute('SELECT * FROM attachments WHERE submission_id=?', (submission['id'],)).fetchall()
            if not attachments:
                raise Conflict('当前完工凭证不完整')
            for photo in attachments:
                path = evidence.safe_path(upload_root, photo['generated_name'])
                if not path.is_file() or hashlib.sha256(path.read_bytes()).hexdigest() != photo['hash']:
                    raise Conflict('完工照片缺失或被修改，不能验收')
            if action == 'reject':
                reason = text_field(data, 'reason', 500)
                changes['status'] = 'returned'
            else:
                if data.get('review_confirmed') != 'yes':
                    raise BadRequest('请确认已检查本轮照片与说明')
                changes.update(status='closed', closed_at=stamp)
            submission_id = submission['id']
            db.execute('UPDATE completion_submissions SET reviewed_by=?,reviewed_at=?,review_result=?,reject_reason=? WHERE id=?',
                       (actor['id'], stamp, 'rejected' if action == 'reject' else 'approved', reason or None, submission_id))
        if changes.get('status', before['status']) != before['status']:
            changes['state_changed_at'] = stamp
        cursor = db.execute('UPDATE tickets SET ' + ','.join(f'{k}=?' for k in changes) + ' WHERE id=? AND version=? AND status=?',
                            (*changes.values(), ticket_id, expected_version, before['status']))
        if cursor.rowcount != 1:
            raise Conflict('并发版本冲突，请刷新')
        after = get_ticket(db, ticket_id)
        event(db, actor, before, after, action, reason, submission_id)
        db.commit()
    except Exception:
        db.rollback()
        for path in written:
            path.unlink(missing_ok=True)
        raise
    return ticket_id
