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
    """Which database we're talking to — this drives the dialect differences.

    The type is only known once a connection has succeeded, so if nothing has
    connected yet we make a throwaway connection to find out. Guessing here is
    what made a SQL Server database get asked MySQL questions, which returned
    nothing at all and looked like an empty database.
    """
    global _DETECTED
    if _DETECTED:
        return _DETECTED
    if DB_TYPE.startswith('post'):
        return 'postgres'
    if DB_TYPE.startswith('my') or DB_TYPE.startswith('maria'):
        return 'mysql'
    if DB_TYPE.startswith('ms') or DB_TYPE.startswith('sql'):
        return 'mssql'
    if configured():
        try:
            _connect().close()          # sets _DETECTED as a side effect
        except Exception:
            pass
    if _DETECTED:
        return _DETECTED
    return 'mssql' if PORT == 1433 else ('postgres' if PORT == 5432 else 'mysql')


def _make_read_friendly(conn, kind_):
    """Make our reads harmless to the live phone system.

    The CMS writes to PhoneCallsLog and AccountWork constantly. A normal read
    takes shared locks on those rows, which collides with those writes — SQL
    Server then kills one side, and it killed us (error 1205, deadlock victim).

    Reading uncommitted means we take no locks at all: we never block the phone
    system and it can never block us. The trade is that a row being written at
    that exact moment may be read mid-change. For a dashboard refreshing every
    fifteen seconds that is the right trade; for anything we ever WRITE, the
    write path sets a proper isolation level of its own.

    A lock timeout is set as well, so a query gives up in seconds rather than
    hanging the page.
    """
    if kind_ != 'mssql':
        return
    try:
        cu = conn.cursor()
        cu.execute('SET TRANSACTION ISOLATION LEVEL READ UNCOMMITTED')
        cu.execute('SET LOCK_TIMEOUT 15000')
        cu.execute('SET DEADLOCK_PRIORITY LOW')   # if there is a fight, we lose it, not the CMS
    except Exception as e:
        print('[cms] could not set read options: ' + str(e)[:120])


def _retrying(fn, attempts=3):
    """Runs a read again if SQL Server killed it as a deadlock victim.

    Deadlocks are transient by nature — the same query usually succeeds a moment
    later. Retrying quietly is better than showing the person a database error
    they can do nothing about.
    """
    import time
    last = None
    for i in range(attempts):
        try:
            return fn()
        except Exception as e:
            msg = str(e)
            last = e
            if '1205' in msg or 'deadlock' in msg.lower():
                time.sleep(0.4 * (i + 1))
                continue
            raise
    raise last


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
            _make_read_friendly(conn, k)
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


def diagnose():
    """Ask SQL Server directly what this login actually is and can see.

    'No columns returned' with no error is the classic signature of two very
    different problems: either the connection is not in the database you think,
    or the login has no permission on any object (SQL Server silently hides
    objects you lack rights to rather than refusing). These questions separate
    them.
    """
    out = {'configured_database': NAME, 'kind': kind()}
    conn = _connect(); cur = conn.cursor()
    k = kind()

    def one(label, sql):
        try:
            cur.execute(sql)
            r = cur.fetchone()
            out[label] = _plain(r[0]) if r else None
        except Exception as e:
            out[label] = 'error: ' + str(e)[:120]

    if k == 'mssql':
        one('connected_to_database', 'SELECT DB_NAME()')
        one('login_name', 'SELECT SUSER_SNAME()')
        one('user_in_database', 'SELECT USER_NAME()')
        one('is_sysadmin', "SELECT IS_SRVROLEMEMBER('sysadmin')")
        one('can_view_definitions', "SELECT HAS_PERMS_BY_NAME(NULL, NULL, 'VIEW ANY DEFINITION')")
        one('tables_visible_sys', 'SELECT COUNT(*) FROM sys.tables')
        one('tables_visible_info', 'SELECT COUNT(*) FROM INFORMATION_SCHEMA.TABLES')
        one('objects_visible', 'SELECT COUNT(*) FROM sys.all_objects')
        one('roles', "SELECT STRING_AGG(r.name, ', ') FROM sys.database_role_members m "
                     "JOIN sys.database_principals r ON r.principal_id = m.role_principal_id "
                     "JOIN sys.database_principals u ON u.principal_id = m.member_principal_id "
                     "WHERE u.name = USER_NAME()")
        # can we switch database at all?
        try:
            cur.execute('USE [%s]' % _safe(NAME))
            cur.execute('SELECT DB_NAME()')
            r = cur.fetchone()
            out['after_use_database'] = _plain(r[0]) if r else None
            cur.execute('SELECT COUNT(*) FROM sys.tables')
            r2 = cur.fetchone()
            out['tables_after_use'] = int(r2[0] or 0) if r2 else 0
        except Exception as e:
            out['after_use_database'] = 'error: ' + str(e)[:140]
            out['tables_after_use'] = None
    else:
        one('connected_to_database', 'SELECT DATABASE()' if k == 'mysql' else 'SELECT current_database()')
        one('login_name', 'SELECT USER()' if k == 'mysql' else 'SELECT current_user')
        one('tables_visible_info', 'SELECT COUNT(*) FROM information_schema.tables')
    conn.close()

    # a plain reading of the result
    if isinstance(out.get('tables_after_use'), int) and out['tables_after_use'] > 0:
        out['meaning'] = ('The login CAN see %d tables once we switch into %s explicitly — '
                          'the connection simply was not in that database. Fixed by switching first.'
                          % (out['tables_after_use'], NAME))
    elif out.get('tables_visible_sys') in (0, '0'):
        out['meaning'] = ('Connected as %s, but SQL Server shows zero tables. That means this login '
                          'has no permission on any object in %s. Ask for db_datareader on the '
                          'database you need to read.'
                          % (out.get('login_name'), out.get('connected_to_database')))
    else:
        out['meaning'] = 'See the values above.'
    return out


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


AGG_FUNCS = {'count': 'COUNT(*)', 'sum': 'SUM', 'avg': 'AVG', 'min': 'MIN', 'max': 'MAX'}
PERIODS = ('day', 'week', 'month', 'quarter', 'year')


def aggregate(table, date_col=None, date_from=None, date_to=None,
              group_col=None, period=None, value_col=None, metric='count',
              database=None, limit=500):
    """Group and total any table — the engine behind 'analyse the orders'.

    Everything is a validated identifier and the dates are parameters, so this
    can answer new questions without anyone writing SQL. Read-only, like the
    rest of this module.

        metric  count | sum | avg | min | max      (sum/avg/min/max need value_col)
        period  day | week | month | quarter | year — bucket the date column
        group_col  any column, e.g. agent, company, status
    """
    metric = (metric or 'count').lower()
    if metric not in AGG_FUNCS:
        raise RuntimeError('metric must be one of: ' + ', '.join(AGG_FUNCS))
    if metric != 'count' and not value_col:
        raise RuntimeError('%s needs a column to work on' % metric)
    if period and period not in PERIODS:
        raise RuntimeError('period must be one of: ' + ', '.join(PERIODS))
    if period and not date_col:
        raise RuntimeError('grouping by %s needs a date column' % period)

    k = kind()
    target = _qualified(table, database)
    q_ = '[%s]' if k == 'mssql' else ('"%s"' if k == 'postgres' else '`%s`')
    for name in (date_col, group_col, value_col):
        if name:
            _safe(name)

    # the time bucket, per dialect
    bucket = None
    if period:
        d = q_ % date_col
        if k == 'mssql':
            bucket = {
                'day':     "CONVERT(varchar(10), %s, 120)" % d,
                'week':    "CONVERT(varchar(4), YEAR(%s)) + '-W' + RIGHT('0' + CONVERT(varchar(2), DATEPART(ISO_WEEK, %s)), 2)" % (d, d),
                'month':   "CONVERT(varchar(7), %s, 120)" % d,
                'quarter': "CONVERT(varchar(4), YEAR(%s)) + '-Q' + CONVERT(varchar(1), DATEPART(QUARTER, %s))" % (d, d),
                'year':    "CONVERT(varchar(4), YEAR(%s))" % d,
            }[period]
        elif k == 'postgres':
            bucket = "to_char(date_trunc('%s', %s), 'YYYY-MM-DD')" % (period, d)
        else:
            bucket = {
                'day':     "DATE_FORMAT(%s, '%%%%Y-%%%%m-%%%%d')" % d,
                'week':    "DATE_FORMAT(%s, '%%%%x-W%%%%v')" % d,
                'month':   "DATE_FORMAT(%s, '%%%%Y-%%%%m')" % d,
                'quarter': "CONCAT(YEAR(%s), '-Q', QUARTER(%s))" % (d, d),
                'year':    "DATE_FORMAT(%s, '%%%%Y')" % d,
            }[period]

    selects, groups, headers = [], [], []
    if bucket:
        selects.append(bucket + ' AS period')
        groups.append(bucket)
        headers.append('period')
    if group_col:
        selects.append((q_ % group_col) + ' AS grp')
        groups.append(q_ % group_col)
        headers.append(group_col)

    agg = 'COUNT(*)' if metric == 'count' else '%s(%s)' % (AGG_FUNCS[metric], q_ % value_col)
    selects.append(agg + ' AS metric')
    headers.append(metric if metric == 'count' else '%s of %s' % (metric, value_col))
    if metric != 'count':
        selects.append('COUNT(*) AS n')
        headers.append('rows')

    where, params = [], []
    if date_col and date_from:
        where.append('%s >= %%s' % (q_ % date_col)); params.append(date_from + ' 00:00:00')
    if date_col and date_to:
        where.append('%s < %%s' % (q_ % date_col)); params.append(date_to + ' 23:59:59')

    sql = 'SELECT %s%s FROM %s' % ('TOP %d ' % int(limit) if k == 'mssql' else '',
                                   ', '.join(selects), target)
    if where:
        sql += ' WHERE ' + ' AND '.join(where)
    if groups:
        sql += ' GROUP BY ' + ', '.join(groups)
        sql += ' ORDER BY ' + (groups[0] if bucket else 'COUNT(*) DESC')
    if k != 'mssql':
        sql += ' LIMIT %d' % int(limit)

    conn = _connect(); cur = conn.cursor()
    cur.execute(sql, tuple(params))
    rows = [[_plain(v) for v in r] for r in cur.fetchall()]
    conn.close()
    total = 0
    try:
        idx = len(headers) - (2 if metric != 'count' else 1)
        total = sum(float(r[idx] or 0) for r in rows)
    except Exception:
        pass
    return {'headers': headers, 'rows': rows, 'total': round(total, 2),
            'sql_shape': sql.replace('%s', '?'), 'table': table,
            'database': database or NAME}


def _searchable_columns(conn, k, db, is_current):
    """Every column we could search in one database, with its type.

    Takes the connection rather than a cursor on purpose: on SQL Server we
    switch database with USE on one cursor and then run the listing on a fresh
    one. Reusing a cursor across a statement that returns no result set is
    exactly the sort of thing that makes a full database look empty, and a new
    cursor costs nothing.

    Returns (rows, note) where note explains an empty result.
    """
    tried = []
    if k == 'mssql':
        try:
            cu = conn.cursor()
            cu.execute('USE [%s]' % _safe(db))
            is_current = True
        except Exception as e:
            tried.append('USE failed: ' + str(e)[:100])

    if k == 'mssql':
        pre = ('[%s].' % _safe(db)) if not is_current else ''
        attempts = [
            ('sys.columns',
             """SELECT t.name, c.name, ty.name FROM %ssys.tables t
                JOIN %ssys.columns c ON c.object_id = t.object_id
                JOIN %ssys.types ty ON ty.user_type_id = c.user_type_id
                ORDER BY t.name""" % (pre, pre, pre), None),
            ('INFORMATION_SCHEMA',
             """SELECT TABLE_NAME, COLUMN_NAME, DATA_TYPE
                FROM %sINFORMATION_SCHEMA.COLUMNS ORDER BY TABLE_NAME""" % pre, None),
            ('sys.all_columns',
             """SELECT o.name, c.name, 'unknown' FROM %ssys.all_columns c
                JOIN %ssys.objects o ON o.object_id = c.object_id
                WHERE o.type = 'U' ORDER BY o.name""" % (pre, pre), None),
        ]
    else:
        attempts = [('information_schema',
                     """SELECT table_name, column_name, data_type
                        FROM information_schema.columns
                        WHERE table_schema = %s ORDER BY table_name""", (db,))]

    for label, sql, prm in attempts:
        try:
            cu = conn.cursor()                     # fresh cursor every attempt
            cu.execute(sql, prm) if prm else cu.execute(sql)
            got = []
            while True:                            # row by row: some drivers
                r = cu.fetchone()                  # dislike fetchall here
                if not r:
                    break
                got.append((r[0], r[1], r[2]))
            if got:
                return got, ''
            tried.append('%s returned 0 rows' % label)
        except Exception as e:
            tried.append('%s: %s' % (label, str(e)[:110]))
    return [], '; '.join(tried) or 'no columns returned'


TOPIC_WORDS = {
    'payments':   ['payment', 'paid', 'amount', 'total', 'price', 'charge', 'card',
                   'transaction', 'balance', 'due', 'refund', 'currency', 'cost', 'fee'],
    'orders':     ['order', 'cart', 'checkout', 'purchase', 'item', 'qty', 'quantity',
                   'sku', 'product', 'shipment', 'delivery'],
    'customers':  ['customer', 'client', 'account', 'contact', 'address', 'email',
                   'phone', 'company', 'billing', 'shipping'],
    'employees':  ['employee', 'agent', 'staff', 'user', 'clock', 'shift', 'payroll',
                   'wage', 'hour', 'extension'],
    'calls':      ['call', 'caller', 'duration', 'recording', 'queue', 'extension',
                   'disposition', 'note'],
    'invoices':   ['invoice', 'bill', 'statement', 'tax', 'subtotal', 'discount',
                   'credit', 'terms'],
    'dates':      ['date', 'created', 'modified', 'updated', 'timestamp', 'time'],
}


SECRET_HINTS = ('key', 'secret', 'token', 'password', 'pwd', 'apikey', 'api_key',
                'privatekey', 'sk_live', 'sk_test', 'auth')


def _mask(name, value):
    """Never hand a live credential to a browser.

    AdminSettings holds the Stripe secret key among ordinary settings. Anyone
    who can read it can take payments, so the value is replaced with its shape:
    enough to tell test from live and to confirm something is set, and useless
    to anyone who copies it.
    """
    v = '' if value is None else str(value)
    looks_secret = any(hw in (name or '').lower() for hw in SECRET_HINTS)
    if looks_secret or v.startswith(('sk_live', 'sk_test', 'rk_live', 'whsec_')):
        if not v:
            return {'value': '', 'masked': True, 'empty': True}
        head = v[:7] if len(v) > 12 else v[:3]
        return {'value': head + '•' * 12, 'masked': True,
                'length': len(v),
                'mode': ('live' if 'live' in v[:8] else 'test' if 'test' in v[:8] else None)}
    return {'value': v, 'masked': False}


def find_recording_base(sample_limit=5):
    """Work out the address recordings are served from.

    The call log only stores a relative path, so the beginning of the address
    has to come from somewhere. Three places are checked, in order of how
    trustworthy they are: a setting in the CMS, any full URL stored elsewhere in
    the database, and finally the addresses the phone system is known to use.
    A real recording path is then tried against each candidate, so the answer is
    proved rather than guessed.
    """
    conn = _connect()
    def rows(sql, prm=()):
        cu = conn.cursor()
        cu.execute(sql, prm) if prm else cu.execute(sql)
        out = []
        while True:
            r = cu.fetchone()
            if not r:
                break
            out.append(r)
        return out

    found_in_db, notes = [], []

    # 1 — a setting that looks like an address
    try:
        for name, val in rows("""SELECT Name, SettingValue FROM AdminSettings
                                 WHERE SettingValue LIKE 'http%' OR Name LIKE '%url%'
                                    OR Name LIKE '%record%' OR Name LIKE '%path%'"""):
            found_in_db.append({'where': 'AdminSettings.' + str(name), 'value': str(val)})
    except Exception as e:
        notes.append('AdminSettings: ' + str(e)[:100])

    # 2 — a full URL stored anywhere in the recording columns
    for tbl, col in (('PhoneCallsLog', 'RecordingFileUrl'),
                     ('PhoneCallsLog', 'RecordingScreenURL'),
                     ('PhoneCallsLog', 'RecordingCamURL'),
                     ('EmployeeRecordings', 'FileUrl'),
                     ('EmployeeRecordings', 'RecordingUrl')):
        try:
            for (v,) in rows("""SELECT TOP 3 %s FROM %s
                                WHERE %s LIKE 'http%%' ORDER BY 1 DESC""" % (col, tbl, col)):
                found_in_db.append({'where': '%s.%s' % (tbl, col), 'value': str(v)})
        except Exception:
            continue

    # a real path to test with
    samples = []
    try:
        for (v,) in rows("""SELECT TOP %d RecordingFileUrl FROM PhoneCallsLog
                            WHERE RecordingFileUrl IS NOT NULL AND RecordingFileUrl <> ''
                            ORDER BY Started DESC""" % int(sample_limit)):
            samples.append(str(v))
    except Exception as e:
        notes.append('sample paths: ' + str(e)[:100])
    conn.close()

    # 3 — addresses the phone system already answers on
    candidates = []
    for f in found_in_db:
        v = f['value']
        if v.lower().startswith('http'):
            base = v.split('/recordings/')[0] if '/recordings/' in v else '/'.join(v.split('/')[:3])
            if base not in candidates:
                candidates.append(base)
    for guess in ('https://panel.myhellodesk.com',
                  'https://cms.myhellodesk.com',
                  'https://myhellodesk.com',
                  'https://panel.myhellodesk.com/media',
                  'https://panel.myhellodesk.com/files'):
        if guess not in candidates:
            candidates.append(guess)

    return {'found_in_database': found_in_db, 'sample_paths': samples,
            'candidates': candidates, 'notes': notes}


def portal_login(username, password):
    """Sign in with a CMS account.

    Looks the person up by user name or email in AspNetUsers, checks the
    password against the stored ASP.NET Identity hash, then finds their Employee
    row and their roles. Read-only: nothing about the account is changed, and
    the password is never stored anywhere.
    """
    import aspnet_auth
    u = (username or '').strip()
    if not u or not password:
        raise RuntimeError('Enter a user name and password')

    conn = _connect(); cu = conn.cursor()
    cu.execute("""SELECT TOP 1 Id, UserName, Email, PasswordHash, LockoutEnabled, LockoutEndDateUtc
                  FROM AspNetUsers WHERE UserName = %s OR Email = %s""", (u, u))
    row = cu.fetchone()
    if not row:
        conn.close()
        raise RuntimeError('No account with that user name')
    user_id, uname, email, phash, lockout_on, lockout_until = row

    if not aspnet_auth.verify_password(password, phash or ''):
        conn.close()
        raise RuntimeError('That password is not right')

    import datetime as _dt
    if lockout_on and lockout_until and lockout_until > _dt.datetime.utcnow():
        conn.close()
        raise RuntimeError('That account is locked in the CMS')

    # who they are on the floor, and whether they do QA
    cu.execute("""SELECT TOP 1 Id, FirstName, LastName, Extension, LeftFirm, QA
                  FROM Employee WHERE AspNetUserId = %s""", (user_id,))
    emp = cu.fetchone()

    # what they are allowed to do, from the CMS's own roles
    roles = []
    try:
        cu.execute("""SELECT r.Name FROM AspNetUserRoles ur
                      JOIN AspNetRoles r ON r.Id = ur.RoleId
                      WHERE ur.UserId = %s""", (user_id,))
        while True:
            r = cu.fetchone()
            if not r:
                break
            roles.append(str(r[0]))
    except Exception:
        pass
    conn.close()

    if emp and emp[4]:
        raise RuntimeError('That person has left the firm')

    # The CMS has exactly three roles: Admin, Manager and Business Account.
    # Admin sits above Manager, and the money pages are reserved for Admin.
    low = [r.lower() for r in roles]
    joined = ' '.join(low)
    is_admin = any(k in joined for k in ('admin', 'owner'))
    is_manager = is_admin or any(k in joined for k in ('manager', 'supervisor'))
    return {
        'user_id': str(user_id),
        'username': uname, 'email': email,
        'employee_id': int(emp[0]) if emp else None,
        'name': (('%s %s' % (emp[1] or '', emp[2] or '')).strip() if emp else (uname or email)),
        'extension': (emp[3] if emp else None),
        'roles': roles,
        'is_manager': is_manager,
        'is_admin': is_admin,
        # the QA flag on the employee record, or a role that says so
        'is_qa': bool(emp[5]) if (emp and len(emp) > 5 and emp[5] is not None) else
                 any('qa' in r.lower() or 'review' in r.lower() for r in roles),
    }


def write_readiness():
    """What this login is allowed to do, and whether a test copy is possible.

    Answers three questions before any write is attempted: can we change data at
    all, can we create a database to practise in, and does a practice copy
    already exist.
    """
    conn = _connect(); cu = conn.cursor()
    out = {'database': NAME}

    def one(label, sql, default=None):
        try:
            cu.execute(sql)
            r = cu.fetchone()
            out[label] = _plain(r[0]) if r else default
        except Exception as e:
            out[label] = 'error: ' + str(e)[:120]

    one('login', 'SELECT SUSER_SNAME()')
    one('is_sysadmin', "SELECT IS_SRVROLEMEMBER('sysadmin')")
    one('can_create_databases', "SELECT IS_SRVROLEMEMBER('dbcreator')")
    one('is_db_owner', "SELECT IS_ROLEMEMBER('db_owner')")
    one('can_insert', "SELECT HAS_PERMS_BY_NAME(NULL, NULL, 'INSERT')")
    one('can_update', "SELECT HAS_PERMS_BY_NAME(NULL, NULL, 'UPDATE')")
    one('can_delete', "SELECT HAS_PERMS_BY_NAME(NULL, NULL, 'DELETE')")

    # is there already a copy to practise on?
    try:
        cu.execute("""SELECT name FROM sys.databases
                      WHERE name LIKE %s OR name LIKE %s ORDER BY name""",
                   (NAME + '_test%', NAME + '_copy%'))
        found = []
        while True:
            r = cu.fetchone()
            if not r:
                break
            found.append(r[0])
        out['existing_copies'] = found
    except Exception as e:
        out['existing_copies'] = []

    conn.close()
    can_write = str(out.get('is_db_owner')) == '1' or str(out.get('can_insert')) == '1'
    out['can_write'] = can_write
    out['meaning'] = (
        ('This login can change data. ' if can_write else 'This login cannot change data. ') +
        ('It can also create databases, so a practice copy can be made from here. '
         if str(out.get('can_create_databases')) == '1'
         else 'It cannot create databases, so the practice copy has to come from the hosting panel. ') +
        ('A copy already exists: ' + ', '.join(out['existing_copies'])
         if out.get('existing_copies') else 'No practice copy exists yet.'))
    return out


def try_write(sql, params=(), commit=False):
    """Run a write and, unless told otherwise, undo it.

    This is the safety net while there is no practice database. The statement
    runs for real inside a transaction — so constraints, triggers and types all
    apply exactly as they would — and is then rolled back. You see how many rows
    it would have touched and any error it would have raised, and the database
    is left untouched.
    """
    conn = _connect()
    out = {'committed': False, 'rows_affected': None, 'dry_run': not commit}
    try:
        cu = conn.cursor()
        # a write needs proper isolation, whatever the read default is
        cu.execute('SET TRANSACTION ISOLATION LEVEL READ COMMITTED')
        cu.execute('SET DEADLOCK_PRIORITY NORMAL')
        cu.execute('BEGIN TRANSACTION')
        cu.execute(sql, tuple(params)) if params else cu.execute(sql)
        try:
            out['rows_affected'] = cu.rowcount
        except Exception:
            pass
        if commit:
            cu.execute('COMMIT TRANSACTION')
            out['committed'] = True
        else:
            cu.execute('ROLLBACK TRANSACTION')
        out['ok'] = True
    except Exception as e:
        out['ok'] = False
        out['error'] = str(e)[:300]
        try:
            conn.cursor().execute('ROLLBACK TRANSACTION')
        except Exception:
            pass
    finally:
        conn.close()
    return out


def settings_all():
    """Every configuration table in the CMS, read only.

    These are the tables that decide how the system behaves — as opposed to the
    ones that record what happened. Each is returned with its rows so it can be
    shown as-is; nothing here is written.
    """
    conn = _connect()
    def rows(sql):
        cu = conn.cursor(); cu.execute(sql)
        cols = [d[0] for d in cu.description] if cu.description else []
        out = []
        while True:
            r = cu.fetchone()
            if not r:
                break
            out.append([_plain(v) for v in r])
        return cols, out

    groups = []

    def add(title, note, sql, transform=None):
        try:
            cols, data = rows(sql)
            if transform:
                cols, data = transform(cols, data)
            groups.append({'title': title, 'note': note, 'columns': cols,
                           'rows': data, 'count': len(data)})
        except Exception as e:
            groups.append({'title': title, 'note': note, 'columns': [], 'rows': [],
                           'count': 0, 'error': str(e)[:160]})

    def mask_admin(cols, data):
        # AdminSettings is Name / SettingValue — mask by the name
        out = []
        for r in data:
            name = r[1] if len(r) > 1 else ''
            m = _mask(name, r[2] if len(r) > 2 else '')
            row = list(r)
            row[2] = m['value'] + ('' if not m['masked'] else
                                   ('   (hidden%s%s)' % (
                                       ', ' + m['mode'] if m.get('mode') else '',
                                       ', %d characters' % m['length'] if m.get('length') else '')))
            out.append(row)
        return cols, out

    add('Admin settings', 'How the system is configured. Secret keys are hidden on purpose.',
        'SELECT Id, Name, SettingValue FROM AdminSettings ORDER BY Name', mask_admin)
    add('Packages sold', 'What customers can buy. Price is in cents.',
        'SELECT Id, Name, Minutes, Price, Currency, Commission, PhoneSystemOption FROM Packages ORDER BY Price')
    add('Payment types', 'How agents are paid.',
        'SELECT Id, Type FROM PaymentType ORDER BY Id')
    add('Call status choices', 'What an agent can mark a call as.',
        'SELECT Id, SelectionText FROM CallStatusSelection ORDER BY Id')
    add('Feedback questions', 'What customers are asked after a call.',
        'SELECT Id, PromptOrder, Description, MaxInput, FileName FROM FeedbackConfig ORDER BY PromptOrder')
    add('Questions', 'Questions attached to calls.',
        'SELECT id, questionDesc, isYesNo, istext, created FROM Question ORDER BY id')
    add('Opening hours', 'The usual week. Day 0 is Sunday.',
        """SELECT s.Id, s.CompanyId, s.DayOfWeek, s.IsOpen, s.OpenTime, s.CloseTime
           FROM CompanyUsualSchedule s ORDER BY s.CompanyId, s.DayOfWeek""")
    add('Special dates', 'Days that differ from the usual week — holidays and closures.',
        'SELECT TOP 100 * FROM CompanySpecialSchedule ORDER BY 1 DESC')
    add('Companies', 'The businesses this system runs for.',
        'SELECT TOP 100 * FROM TableCompanies ORDER BY 1')

    conn.close()
    return {'groups': [g for g in groups if g['count'] or g.get('error')],
            'database': NAME}


MISSED_STATUSES = ('NOANSWER', 'NO ANSWER', 'BUSY', 'CANCEL', 'CONGESTION',
                   'CHANUNAVAIL', 'AFTER HOURS', 'FAILED', 'ABANDON')


def is_missed_status(dial_status):
    """Whether this dial status means the caller did not reach anyone.

    The CMS decides the same way, from the status Asterisk reports when the
    call ends. Outbound calls are never counted as missed.
    """
    s = str(dial_status or '').strip().upper()
    if not s or s.startswith('OUTBOUND'):
        return False
    if s in ('ANSWER', 'ANSWERED'):
        return False
    return any(m in s for m in MISSED_STATUSES)


def missed_calls(hours=48, include_resolved=False, limit=200):
    """Calls nobody answered, newest first.

    A missed call is 'resolved' once the same person gets through afterwards —
    the CMS records that as MissedResolvedId. What matters operationally is the
    unresolved ones: somebody rang, nobody picked up, and they have not been
    called back.
    """
    conn = _connect()
    cu = conn.cursor()
    where = """(ISNULL(c.IsMissed,0) = 1 OR c.DialStatus = 'AFTER HOURS')
               AND c.Started >= DATEADD(hour, -%d, GETDATE())""" % int(hours)
    if not include_resolved:
        where += ' AND ISNULL(c.MissedResolvedId, 0) = 0'

    cu.execute("""SELECT TOP %d c.Id, c.Phone, c.CallersName, c.Started, c.DialStatus,
                         c.CalledExtension, c.MissedResolvedId, c.RequestedCallBack,
                         c.SpecificExten, c.AsteriskStateInfo,
                         a.Id, a.FirstName, a.LastName, ISNULL(a.MinutesLeft, 0)
                  FROM PhoneCallsLog c
                  LEFT JOIN Account a ON RIGHT(REPLACE(REPLACE(ISNULL(a.Phone,''),'-',''),' ',''), 10)
                                       = RIGHT(REPLACE(REPLACE(ISNULL(c.Phone,''),'-',''),' ',''), 10)
                  WHERE %s
                  ORDER BY c.Started DESC""" % (int(limit), where))
    n = lambda v: int(v or 0)
    out = []
    while True:
        r = cu.fetchone()
        if not r:
            break
        out.append({
            'call_id': n(r[0]), 'phone': r[1], 'caller_name': r[2],
            'when': _plain(r[3]), 'status': r[4], 'called': r[5],
            'resolved_by': n(r[6]) or None, 'requested_callback': bool(r[7]),
            'asked_for': r[8],
            'account_id': n(r[10]) or None,
            'account': (('%s %s' % (r[11] or '', r[12] or '')).strip() or None),
            'minutes_left': n(r[13]),
        })

    # how many came in and how many were missed, for the same window
    cu.execute("""SELECT COUNT(*),
                         SUM(CASE WHEN ISNULL(IsMissed,0) = 1 THEN 1 ELSE 0 END),
                         SUM(CASE WHEN ISNULL(IsMissed,0) = 1
                                   AND ISNULL(MissedResolvedId,0) = 0 THEN 1 ELSE 0 END)
                  FROM PhoneCallsLog
                  WHERE Started >= DATEADD(hour, -%d, GETDATE())
                    AND ISNULL(IsOutbound, 0) = 0""" % int(hours))
    t = cu.fetchone() or (0, 0, 0)
    conn.close()
    total, missed, unresolved = n(t[0]), n(t[1]), n(t[2])
    return {'calls': out, 'hours': hours,
            'total_inbound': total, 'missed': missed, 'unresolved': unresolved,
            'missed_rate': (round(missed / total * 100, 1) if total else 0)}


def missed_by_hour(days=7):
    """When calls are being missed — the pattern usually says why."""
    conn = _connect(); cu = conn.cursor()
    cu.execute("""SELECT DATEPART(hour, Started) AS h, COUNT(*),
                         SUM(CASE WHEN ISNULL(IsMissed,0) = 1 THEN 1 ELSE 0 END)
                  FROM PhoneCallsLog
                  WHERE Started >= DATEADD(day, -%d, GETDATE())
                    AND ISNULL(IsOutbound,0) = 0
                  GROUP BY DATEPART(hour, Started)
                  ORDER BY DATEPART(hour, Started)""" % int(days))
    out = []
    while True:
        r = cu.fetchone()
        if not r:
            break
        total, missed = int(r[1] or 0), int(r[2] or 0)
        out.append({'hour': int(r[0] or 0), 'calls': total, 'missed': missed,
                    'rate': round(missed / total * 100, 1) if total else 0})
    conn.close()
    return {'by_hour': out, 'days': days}


def find_account_by_phone(phone):
    """Who is calling — kept in the CMS's three groups, not flattened into one.

    Primary is an account whose main number this is: a confident match. Others
    matched on a mobile, home or other number. Associated are accounts this
    number has been linked to before. The CMS shows them separately so the
    agent can judge, and collapsing them would present a weak match as a
    certain one.
    """
    digits = ''.join(ch for ch in str(phone or '') if ch.isdigit())
    if len(digits) < 7:
        return {'phone': phone, 'primary': None, 'others': [], 'associated': []}
    last10 = digits[-10:]
    conn = _connect()

    def rows(sql, prm):
        cu = conn.cursor()
        cu.execute(sql, prm)
        out = []
        while True:
            r = cu.fetchone()
            if not r:
                break
            out.append({'id': int(r[0]),
                        'name': ('%s %s' % (r[1] or '', r[2] or '')).strip() or '(no name)',
                        'phone': r[3], 'minutes_left': int(r[4] or 0),
                        'business': bool(r[5]) if len(r) > 5 else False})
        return out

    CLEAN = "REPLACE(REPLACE(REPLACE(REPLACE(ISNULL(%s,''),'-',''),' ',''),'(',''),')','')"
    base = """SELECT TOP 10 a.Id, a.FirstName, a.LastName, a.Phone,
                     ISNULL(a.MinutesLeft,0), a.isBusiness
              FROM Account a WHERE ISNULL(a.Deleted,0) = 0 AND """

    primary = rows(base + 'RIGHT(%s, 10) = %%s' % (CLEAN % 'a.Phone'), (last10,))
    others = rows(base + '(RIGHT(%s, 10) = %%s OR RIGHT(%s, 10) = %%s OR RIGHT(%s, 10) = %%s)'
                  % (CLEAN % 'a.Mobile', CLEAN % 'a.HomePhone', CLEAN % 'a.OtherPhone'),
                  (last10, last10, last10))
    associated = []
    try:
        associated = rows("""SELECT TOP 10 a.Id, a.FirstName, a.LastName, a.Phone,
                                    ISNULL(a.MinutesLeft,0), a.isBusiness
                             FROM AccountAssociatedPhoneNumber p
                             JOIN Account a ON a.Id = p.AccountId
                             WHERE RIGHT(%s, 10) = %%s""" % (CLEAN % 'p.Phone'),
                          (last10,))
    except Exception:
        pass

    seen = {a['id'] for a in primary}
    others = [a for a in others if a['id'] not in seen]
    seen |= {a['id'] for a in others}
    associated = [a for a in associated if a['id'] not in seen]

    conn.close()
    return {'phone': phone, 'last10': last10,
            'primary': (primary[0] if primary else None),
            'others': others, 'associated': associated,
            'any': bool(primary or others or associated)}


def active_call_for_extension(extension):
    """The call this agent is on right now, if any.

    Matches their extension as the ringing agent, the person who picked up, or
    a transfer target — the same three ways the CMS looks.
    """
    ext = str(extension or '').strip()
    if not ext:
        return None
    conn = _connect(); cu = conn.cursor()
    cu.execute("""SELECT TOP 1 c.Id, c.Phone, c.Started, c.PickedUpTime, c.Ended,
                         c.IsOutbound, c.DialStatus, c.CalledExtension
                  FROM PhoneCallsLog c
                  WHERE (c.Agent = %s OR c.PickedUpBy = %s)
                    AND c.Started >= DATEADD(hour, -4, GETDATE())
                  ORDER BY c.Id DESC""", (ext, ext))
    r = cu.fetchone()
    conn.close()
    if not r:
        return None
    return {'call_id': int(r[0]), 'phone': r[1], 'started': _plain(r[2]),
            'picked_up': _plain(r[3]), 'ended': _plain(r[4]),
            'outbound': bool(r[5]), 'status': r[6], 'called': r[7],
            'live': r[4] is None}


def associate_number(account_id, phone):
    """Remember that this number belongs to this account.

    The CMS does this when an agent opens an account while on a call from a
    number that matched nothing — which is how a caller from a second phone is
    recognised next time. Without it, the same call is a mystery every time.
    """
    digits = ''.join(ch for ch in str(phone or '') if ch.isdigit())
    if len(digits) < 7:
        return False
    conn = _connect()
    try:
        cu = conn.cursor()
        cu.execute('SET TRANSACTION ISOLATION LEVEL READ COMMITTED')
        cu.execute("""SELECT COUNT(*) FROM AccountAssociatedPhoneNumber
                      WHERE AccountId = %s AND RIGHT(REPLACE(REPLACE(ISNULL(Phone,''),'-',''),' ',''), 10) = %s""",
                   (int(account_id), digits[-10:]))
        if int((cu.fetchone() or [0])[0] or 0) > 0:
            conn.close()
            return False
        cu.execute("""INSERT INTO AccountAssociatedPhoneNumber (AccountId, Phone, Created)
                      VALUES (%s, %s, GETDATE())""", (int(account_id), digits[-10:]))
        conn.commit()
        conn.close()
        return True
    except Exception as e:
        try:
            conn.rollback(); conn.close()
        except Exception:
            pass
        print('[cms] could not link the number: ' + str(e)[:140])
        return False


def customer_search(q, limit=40):
    """Find an account by name, phone or email."""
    q = (q or '').strip()
    if len(q) < 2:
        return []
    like = '%' + q + '%'
    conn = _connect(); cu = conn.cursor()
    cu.execute("""SELECT TOP %d a.Id, a.FirstName, a.LastName, a.Phone, a.Email,
                         a.MinutesLeft, a.isBusiness, a.Deleted,
                         (SELECT COUNT(*) FROM AccountWork w WHERE w.AccountId = a.Id),
                         (SELECT MAX(w2.StartTime) FROM AccountWork w2 WHERE w2.AccountId = a.Id)
                  FROM Account a
                  WHERE a.FirstName + ' ' + a.LastName LIKE %%s OR a.Phone LIKE %%s
                     OR a.Email LIKE %%s OR a.HomePhone LIKE %%s OR a.Mobile LIKE %%s
                  ORDER BY a.LastName, a.FirstName""" % int(limit),
               (like, like, like, like, like))
    out = []
    while True:
        r = cu.fetchone()
        if not r:
            break
        out.append({'id': int(r[0]),
                    'name': ('%s %s' % (r[1] or '', r[2] or '')).strip() or '(no name)',
                    'phone': r[3], 'email': r[4], 'minutes_left': int(r[5] or 0),
                    'business': bool(r[6]), 'deleted': bool(r[7]),
                    'work_count': int(r[8] or 0), 'last_worked': _plain(r[9])})
    conn.close()
    return out


def credential_fields(credential_id):
    """The username and password stored against one customer login.

    Reading this is a privileged act, so the caller records who looked before
    the value is returned — see the route. Nothing is cached here.
    """
    conn = _connect(); cu = conn.cursor()
    cu.execute("""SELECT d.Id, d.Name, d.DataType, d.Value, d.Updated,
                         c.Title, c.Website, c.AccountId
                  FROM CustomerCredentialData d
                  JOIN CustomerCredentials c ON c.Id = d.CredentialsId
                  WHERE d.CredentialsId = %s
                  ORDER BY d.Id""", (int(credential_id),))
    fields, meta = [], {}
    while True:
        r = cu.fetchone()
        if not r:
            break
        meta = {'title': r[5], 'website': r[6], 'account_id': int(r[7] or 0)}
        fields.append({'id': int(r[0]), 'name': r[1], 'type': r[2],
                       'value': r[3], 'updated': _plain(r[4]),
                       'secret': str(r[2] or r[1] or '').lower().find('pass') >= 0})
    conn.close()
    if not fields:
        raise RuntimeError('That login has nothing stored against it.')
    return {'credential_id': int(credential_id), 'fields': fields, **meta}


def company_info():
    """The shared reference list — logins, numbers and other things staff look up.

    Values are returned as they are because using them is the whole point, but
    anything that looks like a password is flagged so the page can keep it
    covered until someone asks to see it. That stops a screen share or a
    passer-by picking up a company password.
    """
    conn = _connect(); cu = conn.cursor()
    cu.execute("""SELECT Id, InfoKey, InfoValue, Created, Updated
                  FROM CompanyInfo ORDER BY InfoKey""")
    out = []
    while True:
        r = cu.fetchone()
        if not r:
            break
        key = (r[1] or '').strip()
        val = (r[2] or '').strip()
        sensitive = any(w in key.lower() for w in
                        ('login', 'password', 'pass', 'pin', 'code', 'key', 'account'))
        out.append({'id': int(r[0]), 'key': key, 'value': val,
                    'sensitive': sensitive,
                    'created': _plain(r[3]), 'updated': _plain(r[4])})
    conn.close()
    return {'items': out}


def packages():
    """What a customer can buy, with the price per minute worked out."""
    conn = _connect(); cu = conn.cursor()
    cu.execute("""SELECT Id, Name, Minutes, Price, Currency, Commission, PhoneSystemOption
                  FROM Packages ORDER BY Price""")
    out = []
    while True:
        r = cu.fetchone()
        if not r:
            break
        mins = int(r[2] or 0)
        cents = int(r[3] or 0)
        out.append({'id': int(r[0]), 'name': (r[1] or '').strip() or '(unnamed)',
                    'minutes': mins, 'price_cents': cents,
                    'currency': (r[4] or 'usd').upper(),
                    'per_minute_cents': round(cents / mins, 2) if mins else None,
                    'commission': int(r[5] or 0), 'phone_option': r[6]})
    conn.close()
    return {'packages': out}


def agent_list(include_left=False):
    """Everyone and their extension — the thing people look up most often."""
    conn = _connect(); cu = conn.cursor()
    cu.execute("""SELECT e.Id, e.FirstName, e.LastName, e.Extension, e.PhoneName,
                         e.LeftFirm, e.Created, e.QA, e.WithCamera, e.Gender,
                         e.ExperienceLevel, e.notLogIn, e.ScheduleId
                  FROM Employee e
                  WHERE %s ORDER BY e.LeftFirm, e.FirstName, e.LastName"""
               % ('1=1' if include_left else 'ISNULL(e.LeftFirm,0) = 0'))
    out = []
    while True:
        r = cu.fetchone()
        if not r:
            break
        out.append({'id': int(r[0]),
                    'name': ('%s %s' % (r[1] or '', r[2] or '')).strip() or '(no name)',
                    'first': r[1], 'last': r[2],
                    'extension': (r[3] or '').strip(),
                    'phone_name': r[4], 'left_firm': bool(r[5]),
                    'since': _plain(r[6]), 'qa': bool(r[7]), 'camera': bool(r[8]),
                    'experience': (int(r[10]) if r[10] is not None else None),
                    'cannot_log_in': bool(r[11]), 'schedule_id': r[12]})
    conn.close()
    return {'agents': out, 'includes_former': include_left}


def recent_accounts(limit=25):
    """Accounts worth seeing without searching: the ones just worked on, and
    the ones just created."""
    conn = _connect()
    def rows(sql):
        cu = conn.cursor(); cu.execute(sql)
        out = []
        while True:
            r = cu.fetchone()
            if not r:
                break
            out.append(r)
        return out
    n = lambda v: int(v or 0)

    worked = [{'id': n(i), 'name': ('%s %s' % (fn or '', ln or '')).strip() or '(no name)',
               'phone': ph, 'minutes_left': n(ml), 'when': _plain(w),
               'agent': ('%s %s' % (efn or '', eln or '')).strip(), 'note': note}
              for i, fn, ln, ph, ml, w, efn, eln, note in rows("""
        SELECT TOP %d a.Id, a.FirstName, a.LastName, a.Phone, a.MinutesLeft,
               w.StartTime, e.FirstName, e.LastName, w.Note
        FROM AccountWork w
        JOIN Account a ON a.Id = w.AccountId
        LEFT JOIN Employee e ON e.Id = w.EmployeeId
        WHERE w.StartTime >= DATEADD(day, -3, GETDATE())
        ORDER BY w.StartTime DESC""" % (int(limit) * 3))]
    # one row per account, keeping the most recent
    seen, worked_unique = set(), []
    for a in worked:
        if a['id'] in seen:
            continue
        seen.add(a['id'])
        worked_unique.append(a)
        if len(worked_unique) >= limit:
            break

    created = [{'id': n(i), 'name': ('%s %s' % (fn or '', ln or '')).strip() or '(no name)',
                'phone': ph, 'email': em, 'when': _plain(cr), 'minutes_left': n(ml),
                'business': bool(b)}
               for i, fn, ln, ph, em, cr, ml, b in rows("""
        SELECT TOP %d Id, FirstName, LastName, Phone, Email, CreatedTime, MinutesLeft, isBusiness
        FROM Account WHERE ISNULL(Deleted,0) = 0
        ORDER BY CreatedTime DESC""" % int(limit))]

    conn.close()
    return {'recently_worked': worked_unique, 'recently_created': created}


def customer_profile(account_id, limit=120):
    """Everything held about one account, in one read.

    Pulls from every table that references Account: the calls worked, the notes
    written at the time, payments, balance movements, texts, tickets, saved
    cards and stored credentials. A recording link is included wherever the
    call log has one.
    """
    aid = int(account_id)
    conn = _connect()

    def rows(sql, prm=()):
        cu = conn.cursor()
        cu.execute(sql, prm) if prm else cu.execute(sql)
        out = []
        while True:
            r = cu.fetchone()
            if not r:
                break
            out.append(r)
        return out

    a = rows("""SELECT Id, FirstName, LastName, Email, Phone, HomePhone, OtherPhone, Mobile,
                       CreatedTime, MinutesLeft, ReviewStatus, Category, Modified, IsFree,
                       Deleted, smsActivate, smsNumber, SMSDateEnd, isBusiness, lastAgent, ContactID
                FROM Account WHERE Id = %s""", (aid,))
    if not a:
        conn.close()
        raise RuntimeError('No account with id %d' % aid)
    r = a[0]
    account = {
        'id': int(r[0]), 'name': ('%s %s' % (r[1] or '', r[2] or '')).strip(),
        'email': r[3], 'phone': r[4], 'home_phone': r[5], 'other_phone': r[6], 'mobile': r[7],
        'created': _plain(r[8]), 'minutes_left': int(r[9] or 0), 'review_status': r[10],
        'category': r[11], 'modified': _plain(r[12]), 'is_free': bool(r[13]),
        'deleted': bool(r[14]), 'sms_active': int(r[15] or 0), 'sms_number': r[16],
        'sms_ends': _plain(r[17]), 'business': bool(r[18]), 'last_agent': r[19],
        'crm_id': r[20],
    }

    # what the account is worth and how much we've worked on it
    t = rows("""SELECT
        (SELECT COUNT(*) FROM AccountWork WHERE AccountId = %s),
        (SELECT ISNULL(SUM(MinutesBilled),0) FROM AccountWork WHERE AccountId = %s),
        (SELECT ISNULL(SUM(AmountPaid),0) FROM PackageSold WHERE AccountId = %s),
        (SELECT ISNULL(SUM(CASE WHEN ISNULL(Refunded,0)=1 THEN ISNULL(RefundAmount, AmountPaid) ELSE 0 END),0)
           FROM PackageSold WHERE AccountId = %s),
        (SELECT COUNT(*) FROM PackageSold WHERE AccountId = %s AND ISNULL(AmountPaid,0) > 0),
        (SELECT ISNULL(SUM(CASE WHEN ISNULL(AmountPaid,0)=0 THEN ISNULL(PackageMinutes,0) ELSE 0 END),0)
           FROM PackageSold WHERE AccountId = %s),
        (SELECT MIN(StartTime) FROM AccountWork WHERE AccountId = %s),
        (SELECT MAX(StartTime) FROM AccountWork WHERE AccountId = %s),
        (SELECT COUNT(*) FROM SMSLog WHERE AccountId = %s),
        (SELECT COUNT(*) FROM AccountNotes WHERE AccountId = %s)
        """, (aid,)*10)[0]
    n = lambda v: int(v or 0)
    totals = {'work_sessions': n(t[0]), 'minutes_billed': n(t[1]),
              'paid_cents': n(t[2]), 'refunded_cents': n(t[3]),
              'collected_cents': n(t[2]) - n(t[3]), 'payments': n(t[4]),
              'free_minutes': n(t[5]), 'first_worked': _plain(t[6]),
              'last_worked': _plain(t[7]), 'sms_count': n(t[8]), 'note_count': n(t[9])}

    # the work log, with the call and its recording alongside
    work = [{'id': n(i), 'start': _plain(st), 'end': _plain(en),
             'agent': ('%s %s' % (fn or '', ln or '')).strip(),
             'minutes_billed': n(mb), 'note': note, 'task': task,
             'paused_sec': n(ps), 'call_id': n(cid),
             'phone': ph, 'direction': ('outbound' if out else 'inbound'),
             'call_started': _plain(cst), 'call_ended': _plain(cen),
             'recording': rec, 'dial_status': ds, 'missed': bool(miss),
             'call_note': cnote}
            for (i, st, en, fn, ln, mb, note, task, ps, cid, ph, out, cst, cen,
                 rec, ds, miss, cnote) in rows("""
        SELECT TOP %d w.Id, w.StartTime, w.EndTime, e.FirstName, e.LastName,
               w.MinutesBilled, w.Note, w.TaskDescription, w.PausedSec,
               w.PhoneCallId, p.Phone, p.IsOutbound, p.Started, p.Ended,
               p.RecordingFileUrl, p.DialStatus, p.IsMissed, p.Note
        FROM AccountWork w
        LEFT JOIN Employee e ON e.Id = w.EmployeeId
        LEFT JOIN PhoneCallsLog p ON p.Id = w.PhoneCallId
        WHERE w.AccountId = %%s ORDER BY w.StartTime DESC""" % int(limit), (aid,))]

    payments_list = [{'when': _plain(cr), 'amount_cents': n(amt), 'minutes': n(mins),
                      'last4': l4, 'note': note, 'refunded': bool(refd),
                      'refund_amount_cents': n(ramt), 'refund_reason': rr,
                      'agent': ('%s %s' % (efn or '', eln or '')).strip(), 'stripe': ch}
                     for (cr, amt, mins, l4, note, refd, ramt, rr, efn, eln, ch) in rows("""
        SELECT TOP 100 ps.Created, ISNULL(ps.AmountPaid,0), ISNULL(ps.PackageMinutes,0),
               ps.Last4, ps.Note, ISNULL(ps.Refunded,0), ISNULL(ps.RefundAmount,0),
               ps.RefundReason, e.FirstName, e.LastName, ps.StripeChargeId
        FROM PackageSold ps LEFT JOIN Employee e ON e.Id = ps.EmployeId
        WHERE ps.AccountId = %s ORDER BY ps.Created DESC""", (aid,))]

    notes = [{'when': _plain(cr), 'note': note,
              'by': ('%s %s' % (fn or '', ln or '')).strip()}
             for cr, note, fn, ln in rows("""
        SELECT TOP 100 n.Created, n.Note, e.FirstName, e.LastName
        FROM AccountNotes n LEFT JOIN Employee e ON e.Id = n.CreatedBy
        WHERE n.AccountId = %s ORDER BY n.Created DESC""", (aid,))]

    balance = [{'when': _plain(cr), 'from': n(pb), 'to': n(nb), 'change': n(nb) - n(pb),
                'reason': reason}
               for cr, pb, nb, reason in rows("""
        SELECT TOP 100 Created, PreviousBalance, NewBalance, AdjustmentReason
        FROM BalanceChangeLog WHERE AccountId = %s ORDER BY Created DESC""", (aid,))]

    sms = [{'when': _plain(d), 'direction': io, 'from_to': cn, 'message': msg, 'media': media}
           for d, io, cn, msg, media in rows("""
        SELECT TOP 100 smsDate, InOut, ContactNumber, message, MediaURL
        FROM SMSLog WHERE AccountId = %s ORDER BY smsDate DESC""", (aid,))]

    tickets = [{'created': _plain(cr), 'status': st, 'priority': pr,
                'subject': subj, 'description': desc, 'closed': _plain(cl)}
               for cr, st, pr, subj, desc, cl in rows("""
        SELECT TOP 50 CreatedTime, Status, Priority, Subject, Description, ClosedTime
        FROM Ticket WHERE AccountId = %s ORDER BY CreatedTime DESC""", (aid,))]

    cards = [{'brand': b, 'last4': l4, 'added': _plain(cr), 'last_used': _plain(lu)}
             for b, l4, cr, lu in rows("""
        SELECT TOP 20 Brand, Last4, Created, LastUsed FROM StripeCustomers
        WHERE AccountId = %s ORDER BY LastUsed DESC""", (aid,))]

    credentials = [{'id': n(i), 'title': ti, 'website': w, 'added': _plain(cr),
                    'fields': n(cnt)}
                   for i, ti, w, cr, cnt in rows("""
        SELECT TOP 40 c.Id, c.Title, c.Website, c.Created,
               (SELECT COUNT(*) FROM CustomerCredentialData d WHERE d.CredentialsId = c.Id)
        FROM CustomerCredentials c
        WHERE c.AccountId = %s ORDER BY c.Created DESC""", (aid,))]

    conn.close()
    return {'account': account, 'totals': totals, 'work': work, 'payments': payments_list,
            'notes': notes, 'balance': balance, 'sms': sms, 'tickets': tickets,
            'cards': cards, 'credentials': credentials}


def _dnd_now(conn):
    """Who is on Do Not Disturb right now.

    NotDistrubStatus is a log of starts and ends, so the current state is the
    most recent row per agent: NOT_DISTRUB means still on it, END_NOT_DISTRUB
    means finished. The note usually says why, which is the part worth seeing.
    """
    out = {}
    try:
        cu = conn.cursor()
        cu.execute("""SELECT n.EmployeeID, n.Status, n.Created, n.Note, n.TypeDND
                      FROM NotDistrubStatus n
                      JOIN (SELECT EmployeeID, MAX(Id) AS Id
                            FROM NotDistrubStatus
                            WHERE Created >= DATEADD(day, -2, GETDATE())
                            GROUP BY EmployeeID) last
                        ON last.Id = n.Id""")
        while True:
            r = cu.fetchone()
            if not r:
                break
            if str(r[1] or '').upper() == 'NOT_DISTRUB':
                out[int(r[0] or 0)] = {'since': _plain(r[2]), 'note': r[3], 'type': r[4]}
    except Exception as e:
        print('[cms] could not read DND status: ' + str(e)[:120])
    return out


def live_snapshot(feed_limit=70, hours=6):
    return _retrying(lambda: _live_snapshot(feed_limit, hours))


def _live_snapshot(feed_limit=70, hours=6):
    """One read of the whole floor: who is on, what is happening on the phones,
    and every recent event in one stream.

    Calls, work sessions and payments are fetched separately then merged by time
    in the caller, so the feed reads like the day actually unfolded.
    """
    conn = _connect()
    def rows(sql, prm=()):
        cu = conn.cursor()
        cu.execute(sql, prm) if prm else cu.execute(sql)
        out = []
        while True:
            r = cu.fetchone()
            if not r:
                break
            out.append(r)
        return out
    n = lambda v: int(v or 0)

    agents = [{
        'employee_id': n(r[0]), 'name': (r[1] or '').strip(), 'extension': r[2],
        # EmployeeClockerStatus in the CMS source is Out = 0, In = 1,
        # OnBreak = 2 — there is no 3 here. (The CMS turns 1 into 3 only when
        # it writes cms_status into the phone system's own table, for an agent
        # on Do Not Disturb.) So 1 and 2 both mean on shift; 3 is accepted in
        # case this column ever carries the phone system's value instead.
        # DND itself is read from the NotDistrubStatus log below.
        'clocker_status': n(r[3]),
        'clocked_in': n(r[3]) in (1, 2, 3),
        'on_break': n(r[3]) == 2,
        'cms_dnd': n(r[3]) == 3,
        'clocker_since': _plain(r[4]), 'break_minutes': n(r[5]),
        'phone': (r[6] or ''), 'phone_since': _plain(r[7]), 'minutes_on_call': n(r[8]),
        'calls_in': n(r[9]), 'calls_out': n(r[10]),
        'last_call_phone': r[11], 'last_call_at': _plain(r[12]),
    } for r in rows("""SELECT EmployeeId, EmployeeName, Extension, ClockerStatus, ClockerTime,
                              BreakMinutes, SipPhoneStatus, SipPhoneTime, MinutesOnCall,
                              NumCallsIn, NumCallsOut, LastCallPhone, LastCallStartAt
                       FROM Tmp_LiveEmployeesStatusFinal""")]

    dnd = _dnd_now(conn)
    for a in agents:
        d = dnd.get(a['employee_id'])
        a['dnd'] = bool(d) or a.get('cms_dnd', False)
        a['dnd_since'] = d['since'] if d else (a.get('clocker_since') if a.get('cms_dnd') else None)
        a['dnd_note'] = (d.get('note') if d else None)
        a['dnd_type'] = (d.get('type') if d else None)

    # calls that have started but not ended — what is happening this second
    in_progress = [{'call_id': n(i), 'phone': ph, 'started': _plain(st),
                    'picked_up': _plain(pu), 'agent_ext': (by or ag or ''),
                    'outbound': bool(ob), 'status': ds, 'called': ce}
                   for i, ph, st, pu, by, ag, ob, ds, ce in rows("""
        SELECT TOP 60 Id, Phone, Started, PickedUpTime, PickedUpBy, Agent, IsOutbound,
               DialStatus, CalledExtension
        FROM PhoneCallsLog
        WHERE Ended IS NULL AND Started >= DATEADD(hour, -4, GETDATE())
        ORDER BY Started DESC""")]

    recent_calls = [{'kind': 'call', 'when': _plain(st), 'call_id': n(i), 'phone': ph,
                     'agent_ext': (by or ag or ''), 'outbound': bool(ob),
                     'missed': bool(miss), 'status': ds, 'recording': rec,
                     'seconds': (int((en - st).total_seconds()) if (en and st) else None)}
                    for i, ph, st, en, by, ag, ob, miss, ds, rec in rows("""
        SELECT TOP %d Id, Phone, Started, Ended, PickedUpBy, Agent, IsOutbound,
               IsMissed, DialStatus, RecordingFileUrl
        FROM PhoneCallsLog WHERE Started >= DATEADD(hour, -%d, GETDATE())
        ORDER BY Started DESC""" % (int(feed_limit), int(hours)))]

    recent_work = [{'kind': 'work', 'when': _plain(st), 'account': ('%s %s' % (fn or '', ln or '')).strip(),
                    'account_id': n(aid), 'agent': ('%s %s' % (efn or '', eln or '')).strip(),
                    'minutes': n(mb), 'note': note, 'ended': _plain(en)}
                   for st, en, aid, fn, ln, efn, eln, mb, note in rows("""
        SELECT TOP %d w.StartTime, w.EndTime, w.AccountId, a.FirstName, a.LastName,
               e.FirstName, e.LastName, w.MinutesBilled, w.Note
        FROM AccountWork w
        LEFT JOIN Account a ON a.Id = w.AccountId
        LEFT JOIN Employee e ON e.Id = w.EmployeeId
        WHERE w.StartTime >= DATEADD(hour, -%d, GETDATE())
        ORDER BY w.StartTime DESC""" % (int(feed_limit), int(hours)))]

    recent_payments = [{'kind': 'payment', 'when': _plain(cr),
                        'account': ('%s %s' % (fn or '', ln or '')).strip(), 'account_id': n(aid),
                        'agent': ('%s %s' % (efn or '', eln or '')).strip(),
                        'amount_cents': n(amt), 'minutes': n(mins), 'last4': l4,
                        'note': note, 'refunded': bool(refd)}
                       for cr, aid, fn, ln, efn, eln, amt, mins, l4, note, refd in rows("""
        SELECT TOP %d ps.Created, ps.AccountId, a.FirstName, a.LastName,
               e.FirstName, e.LastName, ISNULL(ps.AmountPaid,0), ISNULL(ps.PackageMinutes,0),
               ps.Last4, ps.Note, ISNULL(ps.Refunded,0)
        FROM PackageSold ps
        LEFT JOIN Account a ON a.Id = ps.AccountId
        LEFT JOIN Employee e ON e.Id = ps.EmployeId
        WHERE ps.Created >= DATEADD(hour, -%d, GETDATE())
        ORDER BY ps.Created DESC""" % (int(feed_limit), int(hours * 4)))]

    # today's running figures
    d = rows("""SELECT
        (SELECT COUNT(*) FROM PhoneCallsLog WHERE Started >= CAST(GETDATE() AS date)),
        (SELECT COUNT(*) FROM PhoneCallsLog WHERE Started >= CAST(GETDATE() AS date) AND ISNULL(IsMissed,0)=1),
        (SELECT COUNT(*) FROM AccountWork WHERE StartTime >= CAST(GETDATE() AS date)),
        (SELECT ISNULL(SUM(MinutesBilled),0) FROM AccountWork WHERE StartTime >= CAST(GETDATE() AS date)),
        (SELECT ISNULL(SUM(AmountPaid),0) FROM PackageSold WHERE Created >= CAST(GETDATE() AS date)),
        (SELECT COUNT(*) FROM PackageSold WHERE Created >= CAST(GETDATE() AS date) AND ISNULL(AmountPaid,0) > 0),
        (SELECT COUNT(DISTINCT AccountId) FROM AccountWork WHERE StartTime >= CAST(GETDATE() AS date))
        """)[0]
    today = {'calls': n(d[0]), 'missed': n(d[1]), 'work_sessions': n(d[2]),
             'minutes_billed': n(d[3]), 'collected_cents': n(d[4]), 'payments': n(d[5]),
             'accounts_touched': n(d[6])}

    conn.close()
    return {'agents': agents, 'in_progress': in_progress, 'today': today,
            'feed': recent_calls + recent_work + recent_payments}


def agents_live():
    """Who is working right now, from the CMS's own live status table."""
    conn = _connect(); cu = conn.cursor()
    cu.execute("""SELECT EmployeeId, EmployeeName, Extension, ClockerStatus, ClockerTime,
                         BreakMinutes, SipPhoneStatus, SipPhoneTime, MinutesOnCall,
                         NumCallsIn, NumCallsOut, LastCallPhone, LastCallStartAt
                  FROM Tmp_LiveEmployeesStatusFinal ORDER BY EmployeeName""")
    out = []
    while True:
        r = cu.fetchone()
        if not r:
            break
        n = lambda v: int(v or 0)
        out.append({
            'employee_id': n(r[0]), 'name': (r[1] or '').strip(), 'extension': r[2],
            'clocker_status': n(r[3]),
            'clocked_in': n(r[3]) in (1, 2, 3),
            'on_break': n(r[3]) == 2,
            'cms_dnd': n(r[3]) == 3,
            'clocker_since': _plain(r[4]),
            'break_minutes': n(r[5]), 'phone': (r[6] or ''), 'phone_since': _plain(r[7]),
            'minutes_on_call': n(r[8]), 'calls_in': n(r[9]), 'calls_out': n(r[10]),
            'last_call_phone': r[11], 'last_call_at': _plain(r[12]),
        })
    dnd = _dnd_now(conn)
    for a in out:
        d = dnd.get(a['employee_id'])
        a['dnd'] = bool(d) or a.get('cms_dnd', False)
        a['dnd_since'] = d['since'] if d else None
        a['dnd_note'] = (d.get('note') if d else None)
    conn.close()
    return out


def agent_detail(employee_id, date_from, date_to, limit=150):
    """One agent's day-to-day: the calls handled, what was written, what was
    billed, when they clocked in and out, and what they sold."""
    eid = int(employee_id)
    conn = _connect()
    def rows(sql, prm):
        cu = conn.cursor(); cu.execute(sql, prm)
        out = []
        while True:
            r = cu.fetchone()
            if not r:
                break
            out.append(r)
        return out
    span = (date_from + ' 00:00:00', date_to + ' 23:59:59')
    n = lambda v: int(v or 0)

    who = rows("""SELECT Id, FirstName, LastName, Extension, HourlyRate, LeftFirm, Created
                  FROM Employee WHERE Id = %s""", (eid,))
    if not who:
        conn.close()
        raise RuntimeError('No employee with id %d' % eid)
    w = who[0]
    agent = {'id': n(w[0]), 'name': ('%s %s' % (w[1] or '', w[2] or '')).strip(),
             'extension': w[3], 'hourly_rate_cents': n(w[4]), 'left_firm': bool(w[5]),
             'since': _plain(w[6])}

    t = rows("""SELECT COUNT(*), ISNULL(SUM(MinutesBilled),0),
                       COUNT(DISTINCT AccountId), MIN(StartTime), MAX(StartTime),
                       SUM(CASE WHEN ISNULL(Note,'') <> '' THEN 1 ELSE 0 END)
                FROM AccountWork WHERE EmployeeId = %s AND StartTime >= %s AND StartTime < %s""",
             (eid,) + span)[0]
    totals = {'calls_worked': n(t[0]), 'minutes_billed': n(t[1]), 'accounts': n(t[2]),
              'first': _plain(t[3]), 'last': _plain(t[4]), 'with_notes': n(t[5])}
    p = rows("""SELECT COUNT(*), ISNULL(SUM(AmountPaid),0),
                       SUM(CASE WHEN ISNULL(AmountPaid,0)=0 THEN ISNULL(PackageMinutes,0) ELSE 0 END)
                FROM PackageSold WHERE EmployeId = %s AND Created >= %s AND Created < %s""",
             (eid,) + span)[0]
    totals.update({'payments': n(p[0]), 'collected_cents': n(p[1]), 'free_minutes': n(p[2])})

    work = [{'start': _plain(st), 'end': _plain(en), 'account': ('%s %s' % (fn or '', ln or '')).strip(),
             'account_id': n(aid), 'minutes_billed': n(mb), 'note': note, 'task': task,
             'phone': ph, 'recording': rec, 'direction': ('outbound' if out else 'inbound'),
             'missed': bool(miss)}
            for (st, en, fn, ln, aid, mb, note, task, ph, rec, out, miss) in rows("""
        SELECT TOP %d w.StartTime, w.EndTime, a.FirstName, a.LastName, w.AccountId,
               w.MinutesBilled, w.Note, w.TaskDescription, p.Phone, p.RecordingFileUrl,
               p.IsOutbound, p.IsMissed
        FROM AccountWork w
        LEFT JOIN Account a ON a.Id = w.AccountId
        LEFT JOIN PhoneCallsLog p ON p.Id = w.PhoneCallId
        WHERE w.EmployeeId = %%s AND w.StartTime >= %%s AND w.StartTime < %%s
        ORDER BY w.StartTime DESC""" % int(limit), (eid,) + span)]

    clocker = [{'when': _plain(cr), 'inout': (io or '').strip(),
                'break_minutes': n(bm), 'reason': br}
               for cr, io, bm, br in rows("""
        SELECT TOP 200 Created, InOut, BreakMinutes, BreakReason FROM EmployeeClocker
        WHERE EmployeeId = %s AND Created >= %s AND Created < %s ORDER BY Created DESC""",
        (eid,) + span)]

    by_day = [{'day': str(d), 'calls': n(c), 'minutes': n(m)}
              for d, c, m in rows("""
        SELECT CONVERT(varchar(10), StartTime, 120), COUNT(*), ISNULL(SUM(MinutesBilled),0)
        FROM AccountWork WHERE EmployeeId = %s AND StartTime >= %s AND StartTime < %s
        GROUP BY CONVERT(varchar(10), StartTime, 120)
        ORDER BY CONVERT(varchar(10), StartTime, 120)""", (eid,) + span)]

    conn.close()
    return {'agent': agent, 'totals': totals, 'work': work,
            'clocker': clocker, 'by_day': by_day,
            'date_from': date_from, 'date_to': date_to}


def payments(date_from, date_to, employee_id=None, account_id=None,
             account_search=None, recent_limit=60):
    """Everything the payments page shows, from PackageSold.

    Notes on this table, learned from the schema:
      - AmountPaid and RefundAmount are CENTS (Packages.Price is 18900 = $189).
      - A row with AmountPaid = 0 is minutes GIVEN AWAY, not a sale. Those are
        counted separately rather than dropped, because who gives away minutes
        and why is worth seeing.
      - Refunded is a bit; RefundAmount can be null on an older refund, so we
        fall back to the amount paid.
      - The column really is spelled EmployeId.
    """
    k = kind()
    if k != 'mssql':
        raise RuntimeError('the payments view is written for the CMS (SQL Server)')

    where = ['ps.Created >= %s', 'ps.Created < %s']
    params = [date_from + ' 00:00:00', date_to + ' 23:59:59']
    if employee_id:
        where.append('ps.EmployeId = %s'); params.append(int(employee_id))
    if account_id:
        where.append('ps.AccountId = %s'); params.append(int(account_id))
    if account_search:
        where.append("""(a.FirstName + ' ' + a.LastName LIKE %s OR a.Phone LIKE %s
                         OR a.Email LIKE %s)""")
        params += ['%' + account_search + '%'] * 3
    W = ' AND '.join(where)

    FROM = """FROM PackageSold ps
              LEFT JOIN Account a ON a.Id = ps.AccountId
              LEFT JOIN Employee e ON e.Id = ps.EmployeId"""

    # money in cents; a refund with no recorded amount is treated as the full sale
    PAID = 'ISNULL(ps.AmountPaid, 0)'
    REF = "CASE WHEN ISNULL(ps.Refunded,0) = 1 THEN ISNULL(ps.RefundAmount, %s) ELSE 0 END" % PAID
    MIN_SOLD = "CASE WHEN %s > 0 THEN ISNULL(ps.PackageMinutes,0) ELSE 0 END" % PAID
    MIN_FREE = "CASE WHEN %s = 0 THEN ISNULL(ps.PackageMinutes,0) ELSE 0 END" % PAID

    conn = _connect()
    def rows(sql, prm):
        cu = conn.cursor()
        cu.execute(sql, tuple(prm))
        out = []
        while True:
            r = cu.fetchone()
            if not r:
                break
            out.append(r)
        return out

    # ---- headline ----
    t = rows("""SELECT SUM(%s), SUM(%s), SUM(%s), SUM(%s),
                       SUM(CASE WHEN %s > 0 THEN 1 ELSE 0 END),
                       SUM(CASE WHEN %s = 0 THEN 1 ELSE 0 END),
                       SUM(CASE WHEN ISNULL(ps.Refunded,0) = 1 THEN 1 ELSE 0 END),
                       COUNT(DISTINCT ps.AccountId)
                %s WHERE %s""" % (PAID, REF, MIN_SOLD, MIN_FREE, PAID, PAID, FROM, W), params)
    r0 = t[0] if t else [0]*8
    n = lambda v: int(v or 0)
    totals = {
        'gross_cents': n(r0[0]), 'refunded_cents': n(r0[1]),
        'collected_cents': n(r0[0]) - n(r0[1]),
        'minutes_sold': n(r0[2]), 'minutes_free': n(r0[3]),
        'payments': n(r0[4]), 'free_grants': n(r0[5]),
        'refunds': n(r0[6]), 'accounts': n(r0[7]),
    }

    # ---- by month ----
    by_month = [{'month': m, 'collected_cents': n(g) - n(rf), 'refunded_cents': n(rf),
                 'minutes_sold': n(ms), 'minutes_free': n(mf), 'payments': n(cnt)}
                for m, g, rf, ms, mf, cnt in rows(
        """SELECT CONVERT(varchar(7), ps.Created, 120), SUM(%s), SUM(%s), SUM(%s), SUM(%s),
                  SUM(CASE WHEN %s > 0 THEN 1 ELSE 0 END)
           %s WHERE %s
           GROUP BY CONVERT(varchar(7), ps.Created, 120)
           ORDER BY CONVERT(varchar(7), ps.Created, 120)"""
        % (PAID, REF, MIN_SOLD, MIN_FREE, PAID, FROM, W), params)]

    # ---- per agent ----
    by_agent = [{'employee_id': n(eid),
                 'agent': (('%s %s' % (fn or '', ln or '')).strip() or 'Unknown'),
                 'extension': ext, 'collected_cents': n(g) - n(rf), 'refunded_cents': n(rf),
                 'payments': n(cnt), 'minutes_free': n(mf)}
                for eid, fn, ln, ext, g, rf, cnt, mf in rows(
        """SELECT ps.EmployeId, MAX(e.FirstName), MAX(e.LastName), MAX(e.Extension),
                  SUM(%s), SUM(%s), SUM(CASE WHEN %s > 0 THEN 1 ELSE 0 END), SUM(%s)
           %s WHERE %s GROUP BY ps.EmployeId
           ORDER BY SUM(%s) DESC""" % (PAID, REF, PAID, MIN_FREE, FROM, W, PAID), params)]

    # ---- per account ----
    top_accounts = [{'account_id': n(aid),
                     'account': (('%s %s' % (fn or '', ln or '')).strip() or 'Unknown'),
                     'phone': ph, 'collected_cents': n(g) - n(rf), 'payments': n(cnt),
                     'minutes_sold': n(ms), 'last_paid': _plain(last)}
                    for aid, fn, ln, ph, g, rf, cnt, ms, last in rows(
        """SELECT TOP 50 ps.AccountId, MAX(a.FirstName), MAX(a.LastName), MAX(a.Phone),
                  SUM(%s), SUM(%s), SUM(CASE WHEN %s > 0 THEN 1 ELSE 0 END), SUM(%s),
                  MAX(ps.Created)
           %s WHERE %s GROUP BY ps.AccountId
           ORDER BY SUM(%s) DESC""" % (PAID, REF, PAID, MIN_SOLD, FROM, W, PAID), params)]

    # ---- the transactions themselves ----
    recent = [{'id': n(i), 'when': _plain(created),
               'account': (('%s %s' % (fn or '', ln or '')).strip() or 'Unknown'),
               'account_id': n(aid),
               'agent': (('%s %s' % (efn or '', eln or '')).strip() or ''),
               'amount_cents': n(amt), 'minutes': n(mins), 'last4': l4,
               'note': note, 'refunded': bool(refd),
               'refund_amount_cents': n(refamt), 'refund_reason': refreason,
               'stripe': charge}
              for (i, created, aid, fn, ln, efn, eln, amt, mins, l4, note,
                   refd, refamt, refreason, charge) in rows(
        """SELECT TOP %d ps.Id, ps.Created, ps.AccountId, a.FirstName, a.LastName,
                  e.FirstName, e.LastName, %s, ISNULL(ps.PackageMinutes,0), ps.Last4, ps.Note,
                  ISNULL(ps.Refunded,0), ISNULL(ps.RefundAmount,0), ps.RefundReason, ps.StripeChargeId
           %s WHERE %s ORDER BY ps.Created DESC"""
        % (int(recent_limit), PAID, FROM, W), params)]

    # ---- why money went back ----
    refund_reasons = [{'reason': (rr or 'No reason recorded'), 'count': n(cnt), 'cents': n(amt)}
                      for rr, cnt, amt in rows(
        """SELECT ps.RefundReason, COUNT(*), SUM(ISNULL(ps.RefundAmount, %s))
           %s WHERE %s AND ISNULL(ps.Refunded,0) = 1
           GROUP BY ps.RefundReason ORDER BY SUM(ISNULL(ps.RefundAmount, %s)) DESC"""
        % (PAID, FROM, W, PAID), params)]

    # ---- why minutes were given away ----
    free_reasons = [{'reason': (nt or 'No note'), 'count': n(cnt), 'minutes': n(mins)}
                    for nt, cnt, mins in rows(
        """SELECT TOP 20 ps.Note, COUNT(*), SUM(ISNULL(ps.PackageMinutes,0))
           %s WHERE %s AND %s = 0
           GROUP BY ps.Note ORDER BY SUM(ISNULL(ps.PackageMinutes,0)) DESC"""
        % (FROM, W, PAID), params)]

    conn.close()
    return {'totals': totals, 'by_month': by_month, 'by_agent': by_agent,
            'top_accounts': top_accounts, 'recent': recent,
            'refund_reasons': refund_reasons, 'free_reasons': free_reasons,
            'date_from': date_from, 'date_to': date_to}


def payment_people(kind_wanted='agents', search=''):
    """Names for the agent and account pickers."""
    conn = _connect()
    cu = conn.cursor()
    out = []
    like = '%' + (search or '') + '%'
    if kind_wanted == 'agents':
        cu.execute("""SELECT DISTINCT TOP 200 e.Id, e.FirstName, e.LastName, e.Extension
                      FROM Employee e JOIN PackageSold ps ON ps.EmployeId = e.Id
                      WHERE (%s = '' OR e.FirstName + ' ' + e.LastName LIKE %s)
                      ORDER BY e.FirstName""", (search or '', like))
    else:
        cu.execute("""SELECT TOP 60 a.Id, a.FirstName, a.LastName, a.Phone
                      FROM Account a
                      WHERE a.FirstName + ' ' + a.LastName LIKE %s OR a.Phone LIKE %s
                      ORDER BY a.LastName""", (like, like))
    while True:
        r = cu.fetchone()
        if not r:
            break
        out.append({'id': int(r[0]), 'name': ('%s %s' % (r[1] or '', r[2] or '')).strip(),
                    'extra': r[3]})
    conn.close()
    return out


def schema_report(database=None, include_samples=False, sample_values=3):
    """A full map of the database: every table, its columns and types, its keys,
    and — most usefully — how the tables link to each other.

    The relationships matter more than anything else here. Knowing that
    PackageSold.EmployeeId points at Employee.Id is what makes a report
    possible; without it every join is a guess.
    """
    db = database or NAME
    conn = _connect()
    k = kind()
    cur = conn.cursor()

    def rows_of(sql, params=None):
        cu = conn.cursor()
        cu.execute(sql, params) if params else cu.execute(sql)
        out = []
        while True:
            r = cu.fetchone()
            if not r:
                break
            out.append(r)
        return out

    if k == 'mssql':
        try:
            conn.cursor().execute('USE [%s]' % _safe(db))
        except Exception:
            pass

    tables = {}

    # columns
    cols, problem = _searchable_columns(conn, k, db, True)
    if not cols:
        conn.close()
        raise RuntimeError('Could not read the schema of %s — %s' % (db, problem))

    # richer column detail where the dialect allows it
    detail = {}
    try:
        if k == 'mssql':
            for t, cname, ctype, ln, nullable, ident in rows_of(
                    """SELECT t.name, c.name, ty.name, c.max_length, c.is_nullable, c.is_identity
                       FROM sys.tables t
                       JOIN sys.columns c ON c.object_id = t.object_id
                       JOIN sys.types ty ON ty.user_type_id = c.user_type_id"""):
                detail[(t, cname)] = {'type': ty_fmt(ctype, ln), 'nullable': bool(nullable),
                                      'identity': bool(ident)}
        else:
            for t, cname, ctype, nullable in rows_of(
                    """SELECT table_name, column_name, data_type, is_nullable
                       FROM information_schema.columns WHERE table_schema = %s""", (db,)):
                detail[(t, cname)] = {'type': ctype, 'nullable': str(nullable).lower() == 'yes',
                                      'identity': False}
    except Exception:
        pass

    for t, cname, ctype in cols:
        d = tables.setdefault(t, {'table': t, 'columns': [], 'primary_key': [],
                                  'foreign_keys': [], 'referenced_by': [], 'rows': 0})
        info = detail.get((t, cname), {})
        d['columns'].append({'name': cname, 'type': info.get('type', str(ctype)),
                             'nullable': info.get('nullable'), 'identity': info.get('identity')})

    # row counts
    try:
        if k == 'mssql':
            for t, n in rows_of("""SELECT t.name, SUM(p.rows) FROM sys.tables t
                                   JOIN sys.partitions p ON p.object_id = t.object_id
                                    AND p.index_id IN (0,1) GROUP BY t.name"""):
                if t in tables:
                    tables[t]['rows'] = int(n or 0)
        elif k == 'mysql':
            for t, n in rows_of("""SELECT table_name, table_rows FROM information_schema.tables
                                   WHERE table_schema = %s""", (db,)):
                if t in tables:
                    tables[t]['rows'] = int(n or 0)
        else:
            for t, n in rows_of('SELECT relname, n_live_tup FROM pg_stat_user_tables'):
                if t in tables:
                    tables[t]['rows'] = int(n or 0)
    except Exception:
        pass

    # primary keys
    try:
        if k == 'mssql':
            pk_rows = rows_of("""SELECT t.name, c.name FROM sys.indexes i
                                 JOIN sys.index_columns ic ON ic.object_id = i.object_id
                                  AND ic.index_id = i.index_id
                                 JOIN sys.columns c ON c.object_id = ic.object_id
                                  AND c.column_id = ic.column_id
                                 JOIN sys.tables t ON t.object_id = i.object_id
                                 WHERE i.is_primary_key = 1""")
        else:
            pk_rows = rows_of("""SELECT k.table_name, k.column_name
                                 FROM information_schema.table_constraints c
                                 JOIN information_schema.key_column_usage k
                                   ON k.constraint_name = c.constraint_name
                                 WHERE c.constraint_type = 'PRIMARY KEY'
                                   AND c.table_schema = %s""", (db,))
        for t, cname in pk_rows:
            if t in tables:
                tables[t]['primary_key'].append(cname)
    except Exception:
        pass

    # relationships — the part that makes joins possible
    try:
        if k == 'mssql':
            fk_rows = rows_of("""SELECT pt.name, pc.name, rt.name, rc.name, fk.name
                                 FROM sys.foreign_keys fk
                                 JOIN sys.foreign_key_columns fkc ON fkc.constraint_object_id = fk.object_id
                                 JOIN sys.tables pt ON pt.object_id = fkc.parent_object_id
                                 JOIN sys.columns pc ON pc.object_id = fkc.parent_object_id
                                  AND pc.column_id = fkc.parent_column_id
                                 JOIN sys.tables rt ON rt.object_id = fkc.referenced_object_id
                                 JOIN sys.columns rc ON rc.object_id = fkc.referenced_object_id
                                  AND rc.column_id = fkc.referenced_column_id""")
        else:
            fk_rows = rows_of("""SELECT k.table_name, k.column_name,
                                        k.referenced_table_name, k.referenced_column_name,
                                        k.constraint_name
                                 FROM information_schema.key_column_usage k
                                 WHERE k.referenced_table_name IS NOT NULL
                                   AND k.table_schema = %s""", (db,))
        for pt, pc, rt, rc, name in fk_rows:
            if pt in tables:
                tables[pt]['foreign_keys'].append({'column': pc, 'points_to': '%s.%s' % (rt, rc)})
            if rt in tables:
                tables[rt]['referenced_by'].append({'from': '%s.%s' % (pt, pc)})
    except Exception:
        pass

    # optional: a few real values per column, which often explain a column
    # better than its name does
    if include_samples:
        q_ = '[%s]' if k == 'mssql' else ('"%s"' if k == 'postgres' else '`%s`')
        for t, d in list(tables.items()):
            if d['rows'] == 0:
                continue
            try:
                names = [c['name'] for c in d['columns']][:25]
                sel = ', '.join(q_ % n for n in names)
                top = ('SELECT TOP %d %s FROM %s' % (sample_values, sel, q_ % t)) if k == 'mssql' \
                      else ('SELECT %s FROM %s LIMIT %d' % (sel, q_ % t, sample_values))
                got = rows_of(top)
                for i, cname in enumerate(names):
                    vals = [str(_plain(r[i]))[:40] for r in got if r[i] is not None]
                    if vals:
                        for cc in d['columns']:
                            if cc['name'] == cname:
                                cc['examples'] = vals[:sample_values]
            except Exception:
                continue

    conn.close()
    ordered = sorted(tables.values(), key=lambda d: -d['rows'])
    return {'database': db, 'kind': k, 'tables': ordered,
            'table_count': len(ordered),
            'column_count': sum(len(d['columns']) for d in ordered),
            'relationship_count': sum(len(d['foreign_keys']) for d in ordered)}


def ty_fmt(name, max_length):
    """varchar(50) reads better than varchar."""
    n = str(name)
    if n.lower() in ('varchar', 'nvarchar', 'char', 'nchar', 'varbinary', 'binary'):
        try:
            L = int(max_length)
            if L == -1:
                return '%s(max)' % n
            if n.lower().startswith('n'):
                L = L // 2
            return '%s(%d)' % (n, L)
        except Exception:
            pass
    return n


def schema_markdown(rep):
    """The schema as plain text — easy to read, easy to send to someone."""
    L = []
    L.append('# Database: %s  (%s)' % (rep['database'], rep['kind']))
    L.append('%d tables · %d columns · %d relationships'
             % (rep['table_count'], rep['column_count'], rep['relationship_count']))
    L.append('')
    L.append('## Tables by size')
    for d in rep['tables'][:40]:
        L.append('- %s — %s rows, %d columns' % (d['table'], format(d['rows'], ','), len(d['columns'])))
    L.append('')
    for d in rep['tables']:
        L.append('')
        L.append('## %s  (%s rows)' % (d['table'], format(d['rows'], ',')))
        if d['primary_key']:
            L.append('Primary key: %s' % ', '.join(d['primary_key']))
        for c in d['columns']:
            bits = [c['name'], c['type']]
            if c.get('identity'):
                bits.append('identity')
            if c.get('nullable') is False:
                bits.append('required')
            line = '  - %s' % '  '.join(bits)
            if c.get('examples'):
                line += '   e.g. ' + ' | '.join(c['examples'])
            L.append(line)
        if d['foreign_keys']:
            L.append('  Links to:')
            for fk in d['foreign_keys']:
                L.append('    - %s -> %s' % (fk['column'], fk['points_to']))
        if d['referenced_by']:
            L.append('  Linked from: %s' % ', '.join(r['from'] for r in d['referenced_by'][:12]))
    return '\n'.join(L)


def find_topic(topic, database=None, max_tables=60):
    """Find which tables hold a KIND of data — payments, orders, customers.

    Answers "where are the payments?" by looking at column NAMES rather than
    values: a table with amount, paid_date, card_last4 and currency is a
    payments table whatever it happens to be called. Tables are ranked by how
    many matching columns they have and how many rows they hold, because the
    real table is usually both the best match and a big one.
    """
    words = TOPIC_WORDS.get((topic or '').lower())
    if not words:
        words = [w.strip().lower() for w in str(topic or '').split(',') if len(w.strip()) > 2]
    if not words:
        raise RuntimeError('choose a topic, or type your own words separated by commas')

    db = database or NAME
    conn = _connect()
    k = kind()
    cols, problem = _searchable_columns(conn, k, db, db == NAME)
    if problem and not cols:
        conn.close()
        raise RuntimeError('Could not list columns of %s — %s' % (db, problem))

    by_table = {}
    for tname, cname, ctype in cols:
        low = str(cname).lower()
        hit = [w for w in words if w in low]
        if hit:
            d = by_table.setdefault(tname, {'table': tname, 'matched': [], 'score': 0})
            d['matched'].append({'column': cname, 'type': str(ctype), 'because': hit[0]})
            d['score'] += 1

    # row counts make the difference between a lookup table and the real thing
    counts = {}
    try:
        cur = conn.cursor()
        if k == 'mssql':
            cur.execute("""SELECT t.name, SUM(p.rows) FROM sys.tables t
                           JOIN sys.partitions p ON p.object_id = t.object_id
                            AND p.index_id IN (0,1) GROUP BY t.name""")
        elif k == 'mysql':
            cur.execute("""SELECT table_name, table_rows FROM information_schema.tables
                           WHERE table_schema = %s""", (db,))
        else:
            cur.execute('SELECT relname, n_live_tup FROM pg_stat_user_tables')
        while True:
            r = cur.fetchone()
            if not r:
                break
            counts[r[0]] = int(r[1] or 0)
    except Exception:
        pass
    conn.close()

    out = []
    for d in by_table.values():
        d['rows'] = counts.get(d['table'], 0)
        # a table with several matching columns AND real data ranks highest
        d['rank'] = d['score'] * 10 + min(20, (d['rows'] ** 0.25) if d['rows'] > 0 else 0)
        out.append(d)
    out.sort(key=lambda d: -d['rank'])
    return {'topic': topic, 'words': words, 'database': db,
            'tables': out[:max_tables], 'total_matched': len(out),
            'columns_scanned': len(cols)}


def find_value(value, database=None, all_databases=False,
               budget_seconds=150, max_checks=20000, count_matches=True):
    """Find every place a value appears.

    Give it something you can see on a CMS screen — an extension, an order
    number, an email — and it reports every table and column that contains it,
    how many rows match in each, and an example. Optionally across every
    database on the server, not just the configured one.

    Read-only and bounded: each column is probed with a single cheap lookup, and
    the whole thing stops at a time budget so it can't hang the page.
    """
    import time
    value = str(value or '').strip()
    if len(value) < 2:
        raise RuntimeError('give at least two characters to look for')

    k = kind()
    started = time.time()
    numeric = value.lstrip('-').isdigit()
    like = '%' + value + '%'
    TEXT = ('varchar', 'nvarchar', 'char', 'nchar', 'text', 'ntext', 'character varying', 'string')
    NUM = ('int', 'bigint', 'smallint', 'tinyint', 'numeric', 'decimal', 'integer', 'money', 'float', 'real')

    conn = _connect(); cur = conn.cursor()

    # which databases to look through
    if all_databases and k in ('mssql', 'mysql'):
        try:
            targets = [d for d in databases()['databases']
                       if d.lower() not in ('master', 'tempdb', 'model', 'msdb',
                                            'information_schema', 'performance_schema',
                                            'mysql', 'sys')]
        except Exception:
            targets = [database or NAME]
    else:
        targets = [database or NAME]

    hits, checked, skipped, candidates = [], 0, 0, 0
    errors, scanned_dbs, listing_problems = [], [], []
    stopped_early = False

    for db in targets:
        if time.time() - started > budget_seconds:
            stopped_early = True
            break
        is_current = (db == NAME)
        cols, problem = _searchable_columns(conn, k, db, is_current)
        if problem:
            listing_problems.append('%s: %s' % (db, problem))
            continue
        scanned_dbs.append({'database': db, 'columns': len(cols)})

        for tname, cname, ctype in cols:
            if time.time() - started > budget_seconds or checked >= max_checks:
                stopped_early = True
                break
            t = str(ctype).lower()
            is_text = any(x in t for x in TEXT)
            is_num = numeric and any(t == x or t.startswith(x) for x in NUM)
            if not (is_text or is_num):
                continue
            try:
                _safe(tname); _safe(cname)
            except Exception:
                skipped += 1
                continue
            candidates += 1
            target = _qualified(tname, None if is_current else db)
            q_ = '[%s]' if k == 'mssql' else ('"%s"' if k == 'postgres' else '`%s`')
            col = q_ % cname
            try:
                if is_text:
                    sql = ('SELECT TOP 1 %s FROM %s WHERE %s LIKE %%s' % (col, target, col)) if k == 'mssql' \
                          else ('SELECT %s FROM %s WHERE %s LIKE %%s LIMIT 1' % (col, target, col))
                    cur.execute(sql, (like,))
                else:
                    sql = ('SELECT TOP 1 %s FROM %s WHERE %s = %%s' % (col, target, col)) if k == 'mssql' \
                          else ('SELECT %s FROM %s WHERE %s = %%s LIMIT 1' % (col, target, col))
                    cur.execute(sql, (int(value),))
                checked += 1
                row = cur.fetchone()
                if not row:
                    continue

                hit = {'database': db, 'table': tname, 'column': cname,
                       'type': str(ctype), 'example': _plain(row[0]), 'matches': None}
                # only now, on a real hit, is a full count worth paying for
                if count_matches:
                    try:
                        if is_text:
                            cur.execute('SELECT COUNT(*) FROM %s WHERE %s LIKE %%s' % (target, col), (like,))
                        else:
                            cur.execute('SELECT COUNT(*) FROM %s WHERE %s = %%s' % (target, col), (int(value),))
                        hit['matches'] = int(cur.fetchone()[0] or 0)
                    except Exception:
                        pass
                hits.append(hit)
            except Exception as e:
                skipped += 1
                if len(errors) < 8:
                    errors.append('%s.%s.%s: %s' % (db, tname, cname, str(e)[:110]))
                continue

    conn.close()

    # Nothing listed anywhere? Ask the server why, now, rather than making
    # someone click a second button to find out.
    why = None
    if not scanned_dbs:
        try:
            why = diagnose()
        except Exception as e:
            why = {'meaning': 'could not run the permission check: ' + str(e)[:140]}

    hits.sort(key=lambda x: -(x.get('matches') or 0))
    tables = sorted({(h['database'], h['table']) for h in hits})
    return {
        'value': value,
        'database': database or NAME,
        'searched_databases': scanned_dbs,
        'all_databases': bool(all_databases),
        'hits': hits,
        'tables': ['%s.%s' % (d, t) for d, t in tables],
        'columns_found': sum(s['columns'] for s in scanned_dbs),
        'searchable_columns': candidates,
        'columns_checked': checked,
        'columns_skipped': skipped,
        'listing_problems': listing_problems,
        'errors': errors,
        'stopped_early': stopped_early,
        'seconds': round(time.time() - started, 1),
        'why': why,
    }


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
    """Turns a database value into something JSON can carry.

    Opening hours come back as a bare time, which json refuses — that is what
    broke the settings page. uuid and memoryview appear in this database too.
    """
    from datetime import datetime, date, time, timedelta
    from decimal import Decimal
    import uuid as _uuid
    if isinstance(v, (datetime, date)):
        return v.isoformat()
    if isinstance(v, time):
        return v.strftime('%H:%M') if not v.second else v.strftime('%H:%M:%S')
    if isinstance(v, _uuid.UUID):
        return str(v)
    if isinstance(v, memoryview):
        return bytes(v).decode('utf-8', 'replace')
    if isinstance(v, timedelta):
        return str(v)
    if isinstance(v, Decimal):
        return float(v)
    if isinstance(v, (bytes, bytearray)):
        return v.decode('utf-8', 'replace')
    return v
