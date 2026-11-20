import sqlite3
from pathlib import Path

from flask import current_app, g


def connect(path):
    db = sqlite3.connect(str(path), timeout=10, isolation_level=None)
    db.row_factory = sqlite3.Row
    db.execute('PRAGMA foreign_keys=ON')
    db.execute('PRAGMA busy_timeout=10000')
    return db


def get_db():
    if 'db' not in g:
        g.db = connect(current_app.config['DATABASE'])
    return g.db


def close_db(_error=None):
    db = g.pop('db', None)
    if db is not None:
        db.close()


def initialize(app):
    db = connect(app.config['DATABASE'])
    try:
        db.execute('PRAGMA journal_mode=WAL')
        db.executescript(Path(__file__).with_name('schema.sql').read_text(encoding='utf-8'))
        db.execute('BEGIN IMMEDIATE')
        db.executemany('INSERT OR IGNORE INTO users(id,display_name,role) VALUES(?,?,?)', [
            (1, '演示报修人甲', 'reporter'), (2, '演示企服', 'service'),
            (3, '演示工程甲', 'engineer'), (4, '演示工程乙', 'engineer'),
            (5, '演示管理者', 'manager'), (6, '演示报修人乙', 'reporter'),
        ])
        # Only seed once, never reset or overwrite existing tickets on startup.
        if db.execute('SELECT count(*) FROM tickets').fetchone()[0] == 0:
            from .workflow import create_ticket
            actor = dict(db.execute('SELECT * FROM users WHERE id=1').fetchone())
            create_ticket(db, actor, dict(reporter_name='演示报修人甲',
                reporter_contact='DEMO-前台分机', company_name='演示企业', location='演示公共走廊',
                original_text='演示：公共走廊灯不亮。', issue_description='演示：公共走廊灯不亮。'), in_transaction=True)
        db.commit()
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()
