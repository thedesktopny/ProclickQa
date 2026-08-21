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

CONNECT_TIMEOUT = 8


def configured():
    return bool(HOST and NAME and USER)


def _order_to_try():
    """Which database kind to attempt first. The port is a strong hint —
    3306 MySQL/MariaDB, 5432 PostgreSQL, 1433 Microsoft SQL Server. If the type
    was set explicitly we honour it and don't probe at all."""
    if _DETECTED:
        return [_DETECTED]
    t = DB_TYPE
    if t.startswith('post'):
        return ['postgres']
    if t.startswith('my') or t.startswith('maria'):
        return ['mysql']
    if t.startswith('ms') or t.startswith('sql') or t.startswith('mssql'):
        return ['mssql']
    if PORT == 1433:
        return ['mssql', 'mysql', 'postgres']
    if PORT == 5432:
        return ['postgres', 'mysql', 'mssql']
    if PORT == 3306:
        return ['mysql', 'postgres', 'mssql']
    return ['mysql', 'postgres', 'mssql']


def _open(k):
    if k == 'mssql':
        import pymssql
        return pymssql.connect(server=HOST, port=str(PORT), database=NAME,
                               user=USER, password=PASSWORD,
                               login_timeout=CONNECT_TIMEOUT, timeout=30)
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


def reachable(timeout=6):
    """Is the port even open to us? A plain TCP check, so a blocked firewall
    answers in seconds instead of a database driver hanging on a handshake that
    will never complete. Only the first couple of IPv4 addresses are tried, so
    the answer arrives inside the timeout rather than multiplying by however
    many addresses the name resolves to."""
    import socket, time
    t0 = time.time()
    last = 'no address'
    try:
        infos = socket.getaddrinfo(HOST, PORT, socket.AF_INET, socket.SOCK_STREAM)[:2]
    except Exception as e:
        return False, int((time.time() - t0) * 1000), 'name lookup failed: %s' % str(e)[:80]
    for family, stype, proto, _, addr in infos:
        s = socket.socket(family, stype, proto)
        s.settimeout(timeout)
        try:
            s.connect(addr)
            s.close()
            return True, int((time.time() - t0) * 1000), ''
        except socket.timeout:
            last = 'timed out'
        except Exception as e:
            last = str(e)[:120]
        finally:
            try:
                s.close()
            except Exception:
                pass
    return False, int((time.time() - t0) * 1000), last


def test():
    """Can we reach it, and what is it?"""
    out = {'configured': configured(), 'type': DB_TYPE, 'host': HOST, 'port': PORT, 'database': NAME}
    if not configured():
        out['ok'] = False
        out['meaning'] = 'Set CMS_DB_HOST, CMS_DB_NAME, CMS_DB_USER and CMS_DB_PASSWORD first.'
        return out

    # step 1: can we even open the port
    ok, ms, why = reachable()
    out['port_open'] = ok
    out['connect_ms'] = ms
    if not ok:
        out['ok'] = False
        out['error'] = why
        out['meaning'] = ("Could not open %s:%s from this server (%s). Almost always the CMS "
                          "firewall not allowing our address — send them the outbound IPs from "
                          "the Server connections panel. It can also mean a wrong host or port."
                          % (HOST, PORT, why))
        return out

    # step 2: log in
    try:
        conn = _connect()
        c = conn.cursor()
        c.execute({'postgres': 'SELECT version()',
                   'mssql': 'SELECT @@VERSION'}.get(kind(), 'SELECT VERSION()'))
        out['version'] = str(c.fetchone()[0])[:80]
        out['ok'] = True
        out['detected_type'] = kind()
        out['meaning'] = ('Connected — it is %s. Next: find the table holding the clock in/out events.'
                          % {'postgres': 'PostgreSQL', 'mssql': 'Microsoft SQL Server'}
                            .get(kind(), 'MySQL/MariaDB'))
        conn.close()
    except Exception as e:
        out['ok'] = False
        out['error'] = str(e)[:240]
        low = out['error'].lower()
        if 'no module named' in low:
            out['meaning'] = ('The database driver is not installed on the server yet — check that '
                              'requirements.txt was deployed and the app restarted.')
        elif 'access denied' in low or 'authentication' in low or 'password' in low:
            out['meaning'] = 'The port is open, but the username or password was rejected.'
        elif 'unknown database' in low or 'does not exist' in low:
            out['meaning'] = 'Logged in, but that database name does not exist on the server.'
        else:
            out['meaning'] = 'The port is open but the login failed — see the error text.'
    return out


def tables():
    """Every table, with a row estimate — for finding where the clock events live."""
    conn = _connect(); c = conn.cursor()
    if kind() == 'mysql':
        c.execute('SHOW TABLES')
    else:
        c.execute("""SELECT table_name FROM information_schema.tables
                     WHERE table_schema NOT IN ('pg_catalog','information_schema')
                     ORDER BY table_name""")
    names = [r[0] for r in c.fetchall()]
    conn.close()
    # surface the likely candidates first rather than making someone read 200 names
    hints = ('clock', 'attend', 'shift', 'agent', 'employee', 'login', 'session',
             'timesheet', 'status', 'break', 'staff', 'user')
    likely = [n for n in names if any(h in n.lower() for h in hints)]
    return {'tables': names, 'likely': likely, 'count': len(names)}


def databases():
    """Every database on this server. A CMS often keeps different things in
    different databases, so being able to look around beats guessing."""
    conn = _connect(); cur = conn.cursor()
    k = kind()
    if k == 'mssql':
        cur.execute("SELECT name FROM sys.databases WHERE state = 0 ORDER BY name")
    elif k == 'postgres':
        cur.execute("SELECT datname FROM pg_database WHERE datistemplate = false ORDER BY datname")
    else:
        cur.execute('SHOW DATABASES')
    names = [r[0] for r in cur.fetchall()]
    conn.close()
    return {'databases': names, 'current': NAME}


def _qualified(table, database=None):
    """A table name, optionally in another database on the same server."""
    _safe(table)
    k = kind()
    if not database or database == NAME:
        return {'mssql': '[%s]', 'postgres': '"%s"'}.get(k, '`%s`') % table
    _safe(database)
    if k == 'mssql':
        return '[%s]..[%s]' % (database, table)
    if k == 'mysql':
        return '`%s`.`%s`' % (database, table)
    raise RuntimeError('PostgreSQL cannot read another database on the same connection')


def tables_in(database=None):
    """Tables with an approximate row count, biggest first — the big ones are
    usually where the interesting data lives."""
    conn = _connect(); cur = conn.cursor()
    k = kind()
    rows = []
    try:
        if k == 'mssql':
            db = ('[%s].' % _safe(database)) if database and database != NAME else ''
            cur.execute("""SELECT t.name, SUM(p.rows) FROM %ssys.tables t
                           JOIN %ssys.partitions p ON p.object_id = t.object_id AND p.index_id IN (0,1)
                           GROUP BY t.name ORDER BY SUM(p.rows) DESC""" % (db, db))
            rows = [(r[0], int(r[1] or 0)) for r in cur.fetchall()]
        elif k == 'mysql':
            cur.execute("""SELECT table_name, table_rows FROM information_schema.tables
                           WHERE table_schema = %s ORDER BY table_rows DESC""",
                        (database or NAME,))
            rows = [(r[0], int(r[1] or 0)) for r in cur.fetchall()]
        else:
            cur.execute("""SELECT relname, n_live_tup FROM pg_stat_user_tables
                           ORDER BY n_live_tup DESC""")
            rows = [(r[0], int(r[1] or 0)) for r in cur.fetchall()]
    except Exception as e:
        conn.close()
        raise RuntimeError('could not list tables: %s' % str(e)[:160])
    conn.close()
    return {'database': database or NAME, 'count': len(rows),
            'tables': [{'name': n, 'rows': c_} for n, c_ in rows]}


def search(term, database=None):
    """Find any table or column whose name contains the term — the quickest way
    to answer 'where is X kept?' without opening tables one by one."""
    term = str(term or '').strip()
    if len(term) < 2:
        raise RuntimeError('search for at least two characters')
    if not all(ch.isalnum() or ch in '_ -' for ch in term):
        raise RuntimeError('letters, numbers, spaces, - and _ only')
    like = '%' + term + '%'
    conn = _connect(); cur = conn.cursor()
    k = kind()
    db = database or NAME
    if k == 'mssql':
        pre = ('[%s].' % _safe(db)) if db and db != NAME else ''
        cur.execute("""SELECT t.name, c.name FROM %ssys.tables t
                       JOIN %ssys.columns c ON c.object_id = t.object_id
                       WHERE t.name LIKE %%s OR c.name LIKE %%s
                       ORDER BY t.name, c.name""" % (pre, pre), (like, like))
    else:
        cur.execute("""SELECT table_name, column_name FROM information_schema.columns
                       WHERE (table_name LIKE %s OR column_name LIKE %s)
                       ORDER BY table_name, column_name""", (like, like))
    hits = {}
    for t, col in cur.fetchall():
        hits.setdefault(t, []).append(col)
    conn.close()
    return {'term': term, 'database': db, 'matches': len(hits),
            'tables': [{'table': t, 'columns': cols[:40]} for t, cols in sorted(hits.items())]}


def columns(table, database=None):
    """Column names and types for one table."""
    _safe(table)
    conn = _connect(); c = conn.cursor()
    if kind() == 'mysql':
        c.execute('DESCRIBE `%s`' % table)
        cols = [{'name': r[0], 'type': str(r[1])} for r in c.fetchall()]
    else:
        c.execute("""SELECT column_name, data_type FROM information_schema.columns
                     WHERE table_name = %s ORDER BY ordinal_position""", (table,))
        cols = [{'name': r[0], 'type': r[1]} for r in c.fetchall()]
    conn.close()
    return {'table': table, 'columns': cols}


def sample(table, limit=15, database=None):
    """A few rows, so the meaning of each column is obvious at a glance."""
    limit = max(1, min(200, int(limit)))
    target = _qualified(table, database)
    conn = _connect(); c = conn.cursor()
    k = kind()
    if k == 'mssql':
        q = 'SELECT TOP %d * FROM %s' % (limit, target)       # SQL Server has no LIMIT
    else:
        q = 'SELECT * FROM %s LIMIT %d' % (target, limit)
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

    k = kind()
    q_ = '[%s]' if k == 'mssql' else ('"%s"' if k == 'postgres' else '`%s`')
    select = ', '.join(q_ % c for c in cols if c)
    # MySQL, PostgreSQL and SQL Server (via pymssql) all take %s placeholders,
    # so the dates stay parameters and never get pasted into the SQL text.
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
