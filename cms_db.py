"""
CMS database — read Clocker events straight from the source.

Replaces the weekly "export a sheet and upload it" step. VoiceGuard connects to
the CMS database, reads the clock in/out/break events for a date range, and
writes them into its own clocker_events table exactly as the upload does — so
everything downstream (shift reconstruction, overtime, pay, phone deductions)
is unchanged.

Deliberately read-only: only SELECT is ever issued, and the account should be a
read-only one. Nothing here can modify the phone system.

Configure with environment variables on the App Service:
    CMS_DB_TYPE      mysql (default) | postgres
    CMS_DB_HOST      hostname or IP
    CMS_DB_PORT      3306 for MySQL, 5432 for Postgres
    CMS_DB_NAME      database name
    CMS_DB_USER      read-only user
    CMS_DB_PASSWORD  password

The table and column names are NOT hardcoded — they're discovered with the
explorer below and then stored as a mapping in app_settings, because every CMS
names these things differently.
"""
import os

# 'auto' by default: the port and a trial connection tell us what it is, so you
# don't have to know. Set it explicitly only if the guess is ever wrong.
DB_TYPE = (os.getenv('CMS_DB_TYPE') or 'auto').lower()
HOST = os.getenv('CMS_DB_HOST', '')
PORT = int(os.getenv('CMS_DB_PORT') or (5432 if DB_TYPE.startswith('post') else 3306))
_DETECTED = None            # remembered once we know, so we only probe once
NAME = os.getenv('CMS_DB_NAME', '')
USER = os.getenv('CMS_DB_USER', '')
PASSWORD = os.getenv('CMS_DB_PASSWORD', '')

CONNECT_TIMEOUT = 12


def configured():
    return bool(HOST and NAME and USER)


def _order_to_try():
    """Which database kind to attempt first. The port is a strong hint — 3306 is
    MySQL/MariaDB, 5432 is PostgreSQL — and if it is neither we try both."""
    if _DETECTED:
        return [_DETECTED]
    if kind() == 'postgres':
        return ['postgres']
    if DB_TYPE.startswith('my') or DB_TYPE.startswith('maria'):
        return ['mysql']
    if PORT == 5432:
        return ['postgres', 'mysql']
    return ['mysql', 'postgres']


def _open(k):
    if k == 'postgres':
        import psycopg2
        return psycopg2.connect(host=HOST, port=PORT, dbname=NAME, user=USER,
                                password=PASSWORD, connect_timeout=CONNECT_TIMEOUT)
    import pymysql
    return pymysql.connect(host=HOST, port=PORT, database=NAME, user=USER,
                           password=PASSWORD, connect_timeout=CONNECT_TIMEOUT,
                           cursorclass=pymysql.cursors.Cursor)


def kind():
    """Whatever we last connected as — drives the small dialect differences."""
    if _DETECTED:
        return _DETECTED
    return 'postgres' if DB_TYPE.startswith('post') else 'mysql'


def _connect():
    """Opens a read-only-intent connection, working out the database kind if it
    was not specified. Once one works, that answer is reused."""
    global _DETECTED
    if not configured():
        raise RuntimeError('CMS database settings are missing (CMS_DB_HOST / NAME / USER)')
    errors = []
    for k in _order_to_try():
        try:
            conn = _open(k)
            _DETECTED = k
            return conn
        except Exception as e:
            msg = str(e)
            errors.append('%s: %s' % (k, msg[:150]))
            low = msg.lower()
            # A rejected password or a missing database means we already found
            # the right KIND of server — trying the other driver would only
            # produce a confusing second error.
            if any(t in low for t in ('access denied', 'password', 'authentication',
                                      'unknown database', 'does not exist', 'role ')):
                _DETECTED = k
                raise
    raise RuntimeError(' | '.join(errors))


def test():
    """Can we reach it, and what is it?"""
    out = {'configured': configured(), 'type': DB_TYPE, 'host': HOST, 'port': PORT, 'database': NAME}
    if not configured():
        out['ok'] = False
        out['meaning'] = 'Set CMS_DB_HOST, CMS_DB_NAME, CMS_DB_USER and CMS_DB_PASSWORD first.'
        return out
    try:
        conn = _connect()
        c = conn.cursor()
        c.execute('SELECT version()' if kind() == 'postgres' else 'SELECT VERSION()')
        out['version'] = str(c.fetchone()[0])[:80]
        out['ok'] = True
        out['detected_type'] = kind()
        out['meaning'] = ('Connected — it is %s. Next: find the table holding the clock in/out events.'
                          % ('PostgreSQL' if kind() == 'postgres' else 'MySQL/MariaDB'))
        conn.close()
    except Exception as e:
        out['ok'] = False
        out['error'] = str(e)[:240]
        low = out['error'].lower()
        if 'timed out' in low or 'timeout' in low or "can't connect" in low:
            out['meaning'] = ("Could not reach the server. Most likely our IP isn't allowed through "
                              "its firewall — send them the outbound addresses from the panel above.")
        elif 'access denied' in low or 'authentication' in low or 'password' in low:
            out['meaning'] = 'Reached the server, but the username or password was rejected.'
        elif 'unknown database' in low or 'does not exist' in low:
            out['meaning'] = 'Reached the server and logged in, but that database name does not exist.'
        else:
            out['meaning'] = 'Connection failed — see the error text.'
    return out


def tables():
    """Every table, with a row estimate — for finding where the clock events live."""
    conn = _connect(); c = conn.cursor()
    if kind() == 'postgres':
        c.execute("""SELECT table_name FROM information_schema.tables
                     WHERE table_schema NOT IN ('pg_catalog','information_schema')
                     ORDER BY table_name""")
    else:
        c.execute('SHOW TABLES')
    names = [r[0] for r in c.fetchall()]
    conn.close()
    # surface the likely candidates first rather than making someone read 200 names
    hints = ('clock', 'attend', 'shift', 'agent', 'employee', 'login', 'session',
             'timesheet', 'status', 'break', 'staff', 'user')
    likely = [n for n in names if any(h in n.lower() for h in hints)]
    return {'tables': names, 'likely': likely, 'count': len(names)}


def columns(table):
    """Column names and types for one table."""
    _safe(table)
    conn = _connect(); c = conn.cursor()
    if kind() == 'postgres':
        c.execute("""SELECT column_name, data_type FROM information_schema.columns
                     WHERE table_name = %s ORDER BY ordinal_position""", (table,))
        cols = [{'name': r[0], 'type': r[1]} for r in c.fetchall()]
    else:
        c.execute('DESCRIBE `%s`' % table)
        cols = [{'name': r[0], 'type': str(r[1])} for r in c.fetchall()]
    conn.close()
    return {'table': table, 'columns': cols}


def sample(table, limit=15):
    """A few rows, so the meaning of each column is obvious at a glance."""
    _safe(table)
    limit = max(1, min(100, int(limit)))
    conn = _connect(); c = conn.cursor()
    q = ('SELECT * FROM "%s" LIMIT %d' % (table, limit)) if kind() == 'postgres' \
        else ('SELECT * FROM `%s` LIMIT %d' % (table, limit))
    c.execute(q)
    names = [d[0] for d in c.description]
    rows = [[_plain(v) for v in r] for r in c.fetchall()]
    conn.close()
    return {'table': table, 'columns': names, 'rows': rows}


def fetch_events(mapping, date_from, date_to):
    """Read clock events for a date range using the discovered column mapping.

    mapping = {
        'table':        'agent_clocker',
        'employee':     'EmployeeId',      # who
        'time':         'Created',         # when
        'status':       'Status',          # In / OnBreak / Out
        'break_minutes':'BreakMinutes',    # optional
        'break_reason': 'BreakReason',     # optional
    }
    """
    for key in ('table', 'employee', 'time', 'status'):
        if not mapping.get(key):
            raise RuntimeError('mapping is missing "%s"' % key)
    t = mapping['table']
    _safe(t)
    cols = [mapping['employee'], mapping['time'], mapping['status']]
    for opt in ('break_minutes', 'break_reason'):
        cols.append(mapping.get(opt) or None)
    for cname in cols:
        if cname:
            _safe(cname)

    q_ = '"%s"' if kind() == 'postgres' else '`%s`'
    select = ', '.join(q_ % c for c in cols if c)
    sql = ('SELECT %s FROM %s WHERE %s >= %%s AND %s < %%s ORDER BY %s'
           % (select, q_ % t, q_ % mapping['time'], q_ % mapping['time'], q_ % mapping['time']))

    conn = _connect(); c = conn.cursor()
    c.execute(sql, (date_from + ' 00:00:00', date_to + ' 23:59:59'))
    raw = c.fetchall()
    conn.close()

    has_bm = bool(mapping.get('break_minutes'))
    has_br = bool(mapping.get('break_reason'))
    events = []
    for r in raw:
        i = 3
        bm = r[i] if has_bm else None
        if has_bm:
            i += 1
        br = r[i] if has_br else None
        events.append({
            'employee_name': str(r[0]).strip(),
            'event_time': _plain(r[1]),
            'status': str(r[2]).strip(),
            'break_minutes': float(bm) if bm not in (None, '') else None,
            'break_reason': str(br).strip() if br not in (None, '') else None,
        })
    return events


def _safe(name):
    """Table and column names come from a saved mapping, not from a user typing
    SQL — this makes sure nothing but a plain identifier can ever reach a query."""
    import re
    if not re.fullmatch(r'[A-Za-z0-9_$]{1,64}', str(name or '')):
        raise RuntimeError('unsafe table or column name: %r' % name)
    return name


def _plain(v):
    from datetime import datetime, date, timedelta
    from decimal import Decimal
    if isinstance(v, (datetime, date)):
        return v.isoformat()
    if isinstance(v, timedelta):
        return str(v)
    if isinstance(v, Decimal):
        return float(v)
    if isinstance(v, (bytes, bytearray)):
        return v.decode('utf-8', 'replace')
    return v
