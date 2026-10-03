import sqlite3
from datetime import datetime, timedelta, timezone

import pytest

from app.integrations.mac_messages import NaturalTestSessionMessagesReader, MacWorker

NOW = datetime(2026, 10, 3, 16, 30, tzinfo=timezone.utc)
PHONE = '+15555550101'
LINE = '+15555550200'
SESSION = {'id': 'a' * 32, 'starts_at': (NOW-timedelta(minutes=5)).isoformat(),
           'expires_at': (NOW+timedelta(minutes=5)).isoformat()}


@pytest.fixture
def reader(tmp_path):
    path = tmp_path / 'messages.db'
    db = sqlite3.connect(path)
    db.executescript('''
        CREATE TABLE message(guid,text,attributedBody,service,is_from_me,handle_id,destination_caller_id,date);
        CREATE TABLE handle(id);
        CREATE TABLE chat(guid,service_name,last_addressed_handle);
        CREATE TABLE chat_message_join(message_id,chat_id);
        CREATE TABLE chat_handle_join(chat_id,handle_id);
    ''')
    db.executemany('INSERT INTO handle VALUES (?)', [(PHONE,), ('+15555550999',)])
    db.executemany('INSERT INTO chat VALUES (?,?,?)', [('direct','iMessage',LINE), ('other','iMessage',LINE),
        ('wrong-line','iMessage','+15555550300'), ('group','iMessage',LINE)])
    db.executemany('INSERT INTO chat_handle_join VALUES (?,?)', [(1,1),(2,2),(3,1),(4,1),(4,2)])
    epoch = datetime(2001,1,1,tzinfo=timezone.utc)

    def add(body='Synthetic Person', *, chat=1, handle=1, line=LINE, when=NOW, blob=None, outgoing=0):
        row = db.execute('INSERT INTO message VALUES (?,?,?,?,?,?,?,?)',
            ('guid-'+str(db.execute('SELECT COUNT(*) FROM message').fetchone()[0]),body,blob,
             'iMessage',outgoing,handle,line,int((when-epoch).total_seconds()*1e9))).lastrowid
        db.execute('INSERT INTO chat_message_join VALUES (?,?)',(row,chat))
        db.commit()
        return row

    current = [NOW]
    r = NaturalTestSessionMessagesReader(path,{PHONE},'/synthetic/helper',LINE,('iMessage',),
        {PHONE:SESSION},now=lambda:current[0])
    yield r, add, current
    r.connection.close()
    db.close()


def test_natural_reply_needs_no_marker_and_is_bound_to_exact_route(reader, monkeypatch):
    r,add,current = reader
    first = add()
    add(chat=2,handle=2)
    add(chat=3,line='+15555550300')
    add(chat=4)
    add(when=NOW-timedelta(hours=1))
    add(outgoing=1)
    decoded = []
    monkeypatch.setattr('app.integrations.mac_messages.decode_body',lambda blob,helper: decoded.append(blob) or 'YES')
    add(body=None,blob=b'allowed')
    add(body=None,blob=b'private',chat=2,handle=2)
    result = r.new_messages(0)
    assert [row['body'] for row in result] == ['Synthetic Person','YES']
    assert all(row['session_id'] == SESSION['id'] for row in result)
    assert decoded == [b'allowed']
    assert [row['body'] for row in r.new_messages(first)] == ['YES']


def test_expired_session_reads_only_stop_and_attachments_are_skipped(reader):
    r,add,current = reader
    add(body=None)
    assert r.new_messages(0)[0]['skip'] is True
    current[0] = NOW+timedelta(minutes=6)
    add(body='Personal conversation',when=current[0])
    add(body='STOP',when=current[0])
    result = r.new_messages(0)
    assert [row['body'] for row in result] == ['STOP']


def test_worker_rejects_unknown_input_mode_before_reading_messages(tmp_path):
    with pytest.raises(ValueError,match='input_mode'):
        MacWorker({'phones':[PHONE],'receiving_number':LINE,'test_sessions':{PHONE:SESSION},
            'input_mode':'unbounded','state_path':str(tmp_path/'checkpoint')})
