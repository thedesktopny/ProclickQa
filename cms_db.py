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
            cur.execute('SELECT DB_NAME(), COUNT(*) FROM sys.tables')
            r = cur.fetchone()
            out['after_use_database'] = _plain(r[0])
            out['tables_after_use'] = int(r[1] or 0)
        except Exception as e:
            out['after_use_database'] = 'error: ' + str(e)[:140]
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


def _searchable_columns(cur, k, db, is_current):
    """Every column we could search in one database, with its type.

    On SQL Server we switch into the database with USE first. Three-part names
    work only when the login has rights in the other database, and a connection
    is not always in the database you asked for — switching explicitly removes
    both doubts.
    """
    if k == 'mssql':
        try:
            cur.execute('USE [%s]' % _safe(db))
            try:
                cur.fetchall()
            except Exception:
                pass
            is_current = True          # we are now genuinely in it
        except Exception:
            pass
    if k == 'mssql':
        pre = ('[%s].' % _safe(db)) if not is_current else ''
        attempts = [
            ("""SELECT t.name, c.name, ty.name FROM %ssys.tables t
                JOIN %ssys.columns c ON c.object_id = t.object_id
                JOIN %ssys.types ty ON ty.user_type_id = c.user_type_id
                ORDER BY t.name""" % (pre, pre, pre), None),
            ("""SELECT TABLE_NAME, COLUMN_NAME, DATA_TYPE FROM %sINFORMATION_SCHEMA.COLUMNS
                ORDER BY TABLE_NAME""" % pre, None),
        ]
    else:
        attempts = [("""SELECT table_name, column_name, data_type
                        FROM information_schema.columns
                        WHERE table_schema = %s ORDER BY table_name""", (db,))]
    last = ''
    for sql, prm in attempts:
        try:
            cur.execute(sql, prm) if prm else cur.execute(sql)
            got = cur.fetchall()
            if got:
                return got, ''
        except Exception as e:
            last = str(e)[:160]
    return [], (last or 'no columns returned')


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
        cols, problem = _searchable_columns(cur, k, db, is_current)
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
