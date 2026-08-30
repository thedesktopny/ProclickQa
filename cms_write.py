"""
Writing back to the CMS.

Everything that changes data goes through here, for three reasons:

  * only the tables and columns listed below can be touched at all, so a bug
    somewhere else cannot reach the phone system's data;
  * every change is written to an audit log in VoiceGuard's own database —
    who, when, what it was before, what it became — so a mistake can be found
    and undone;
  * a change can be rehearsed first. The statement runs for real inside a
    transaction and is rolled back, which proves the constraints and types are
    right without keeping anything.

The CMS application enforces rules we cannot see, so this deliberately covers
only records that stand alone. Payments, minute balances and logins are not
here: those touch several tables at once and belong to the CMS.
"""
import cms_db


# ---------------------------------------------------------------- allow-list --
# table -> (columns that may be written, columns required when creating)
WRITABLE = {
    'Account': (
        {'FirstName', 'LastName', 'Email', 'Phone', 'HomePhone', 'OtherPhone',
         'Mobile', 'Category', 'isBusiness', 'IsFree', 'ReviewStatus',
         'smsActivate', 'smsNumber', 'lastAgent'},
        {'FirstName'},
    ),
    'AccountNotes': (
        {'AccountId', 'Note', 'CreatedBy'},
        {'AccountId', 'Note'},
    ),
}

# columns the database fills in itself — never sent from a form
SERVER_SET = {
    'Account': {'CreatedTime': 'GETDATE()', 'Modified': 'GETDATE()'},
    'AccountNotes': {'Created': 'GETDATE()'},
}

MAX_TEXT = 400


def _explain(e):
    """Turns a database complaint into something a person can act on."""
    m = str(e)
    low = m.lower()
    if 'permission' in low or 'denied' in low:
        return ('The database refused the change — this login may only be able to read. '
                'Original message: ' + m[:200])
    if 'foreign key' in low or 'reference' in low:
        return ('That points at a record which does not exist. ' + m[:200])
    if 'truncat' in low or 'too long' in low:
        return ('One of the values is longer than the column allows. ' + m[:200])
    if 'cannot insert the value null' in low or 'does not allow nulls' in low:
        return ('A field the CMS requires was left empty. ' + m[:200])
    if 'conversion' in low or 'converting' in low:
        return ('A value is the wrong type for its column. ' + m[:200])
    if 'deadlock' in low or '1205' in m:
        return 'The phone system was writing at the same moment. Try again.'
    if 'identity_insert' in low:
        return 'The Id column cannot be set by hand.'
    return m[:280]


class WriteRefused(Exception):
    """The change was not allowed. The message says why, in plain terms."""


def _check(table, values, creating):
    if table not in WRITABLE:
        raise WriteRefused('%s is not a table this system may change.' % table)
    allowed, required = WRITABLE[table]
    clean = {}
    for k, v in (values or {}).items():
        if k not in allowed:
            raise WriteRefused('%s cannot be changed here.' % k)
        if isinstance(v, str):
            v = v.strip()
            if len(v) > MAX_TEXT:
                raise WriteRefused('%s is too long (%d characters).' % (k, len(v)))
        clean[k] = v
    if creating:
        missing = [r for r in required if not str(clean.get(r, '')).strip()]
        if missing:
            raise WriteRefused('Still needed: ' + ', '.join(missing))
    if not clean:
        raise WriteRefused('Nothing was filled in.')
    return clean


def _row_now(conn, table, row_id):
    """What the row looks like before we touch it — the 'before' in the log."""
    cu = conn.cursor()
    cu.execute('SELECT * FROM [%s] WHERE Id = %%s' % cms_db._safe(table), (int(row_id),))
    cols = [d[0] for d in cu.description]
    r = cu.fetchone()
    return dict(zip(cols, [cms_db._plain(v) for v in r])) if r else None


def create(table, values, who, dry_run=True):
    """Add a row. Returns the new id, or what would have happened."""
    clean = _check(table, values, creating=True)
    server = SERVER_SET.get(table, {})
    cols = list(clean.keys())
    placeholders = ['%s'] * len(cols)
    for col, expr in server.items():
        cols.append(col); placeholders.append(expr)

    sql = ('INSERT INTO [%s] (%s) OUTPUT INSERTED.Id VALUES (%s)'
           % (cms_db._safe(table),
              ', '.join('[%s]' % cms_db._safe(c) for c in cols),
              ', '.join(placeholders)))
    params = [clean[c] for c in clean]

    conn = cms_db._connect()
    out = {'table': table, 'action': 'create', 'values': clean,
           'dry_run': dry_run, 'sql': sql}
    try:
        cu = conn.cursor()
        cu.execute('SET TRANSACTION ISOLATION LEVEL READ COMMITTED')
        cu.execute(sql, tuple(params))
        r = cu.fetchone()
        out['new_id'] = int(r[0]) if r else None
        # The driver holds its own transaction. Committing in SQL alone leaves
        # that one open, and closing the connection then throws the work away —
        # which looked exactly like a successful save that changed nothing.
        if dry_run:
            conn.rollback()
            out['committed'] = False
            out['meaning'] = ('This would create %s #%s. Nothing has been saved.'
                              % (table, out.get('new_id')))
            out['ok'] = True
        else:
            conn.commit()
            # read it back — a new row that cannot be found was not created
            check = _row_now(conn, table, out['new_id']) if out.get('new_id') else None
            if check is None:
                out['ok'] = False
                out['committed'] = False
                out['error'] = ('The database reported success but the new record cannot be '
                                'found. The login may not have permission to write.')
            else:
                out['committed'] = True
                out['ok'] = True
                out['meaning'] = 'Created %s #%s.' % (table, out.get('new_id'))
    except Exception as e:
        try:
            conn.cursor().execute('ROLLBACK TRANSACTION')
        except Exception:
            pass
        out.update({'ok': False, 'committed': False,
                    'error': _explain(e), 'raw_error': str(e)[:300]})
    finally:
        conn.close()
    _log(who, out, before=None)
    return out


def update(table, row_id, values, who, dry_run=True):
    """Change a row. The previous values are recorded before anything moves."""
    clean = _check(table, values, creating=False)
    server = SERVER_SET.get(table, {})
    sets = ['[%s] = %%s' % cms_db._safe(c) for c in clean]
    if 'Modified' in server:
        sets.append('[Modified] = GETDATE()')
    sql = ('UPDATE [%s] SET %s WHERE Id = %%s'
           % (cms_db._safe(table), ', '.join(sets)))
    params = list(clean.values()) + [int(row_id)]

    conn = cms_db._connect()
    out = {'table': table, 'action': 'update', 'row_id': int(row_id),
           'values': clean, 'dry_run': dry_run, 'sql': sql}
    before = None
    try:
        before = _row_now(conn, table, row_id)
        if before is None:
            raise WriteRefused('There is no %s with id %s.' % (table, row_id))
        out['before'] = {k: before.get(k) for k in clean}
        cu = conn.cursor()
        cu.execute('SET TRANSACTION ISOLATION LEVEL READ COMMITTED')
        cu.execute(sql, tuple(params))
        affected = cu.rowcount
        out['rows_affected'] = None if affected is None or affected < 0 else affected
        if dry_run:
            conn.rollback()
            out['committed'] = False
            n = out['rows_affected']
            out['meaning'] = ('This would change %s. Nothing has been saved.'
                              % ('1 row' if n in (None, 1) else '%d rows' % n))
            out['ok'] = True
        else:
            conn.commit()
            # Prove it. Reading the row back is the only honest way to say
            # "saved" — the driver reporting success is not the same thing.
            after = _row_now(conn, table, row_id)
            changed = {k: after.get(k) for k in clean} if after else {}
            wanted = {k: (str(v) if v is not None else None) for k, v in clean.items()}
            got = {k: (str(v) if v is not None else None) for k, v in changed.items()}
            mismatched = [k for k in wanted
                          if k not in ('isBusiness', 'IsFree') and wanted[k] != got.get(k)]
            out['after'] = changed
            if mismatched:
                out['ok'] = False
                out['committed'] = False
                out['error'] = ('The database accepted the change but the record still reads '
                                'the same (%s). The login may not have permission to write.'
                                % ', '.join(mismatched))
            else:
                out['committed'] = True
                out['ok'] = True
                out['meaning'] = 'Saved and confirmed.'
    except WriteRefused:
        conn.close()
        raise
    except Exception as e:
        try:
            conn.cursor().execute('ROLLBACK TRANSACTION')
        except Exception:
            pass
        out.update({'ok': False, 'committed': False,
                    'error': _explain(e), 'raw_error': str(e)[:300]})
    finally:
        try:
            conn.close()
        except Exception:
            pass
    _log(who, out, before=out.get('before'))
    return out


def _log(who, result, before=None):
    """Records the change in VoiceGuard's own database.

    Kept here rather than in the CMS on purpose: the CMS should carry only what
    its own application expects, and an audit trail we control is one we can
    trust and query.
    """
    if result.get('dry_run'):
        return                      # a rehearsal is not a change
    try:
        import json
        from server import get_db
        conn = get_db(); c = conn.cursor()
        c.execute("""INSERT INTO cms_audit
                     (who, employee_id, action, table_name, row_id, before_json,
                      after_json, committed, error)
                     VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)""",
                  ((who or {}).get('name'), (who or {}).get('employee_id'),
                   result.get('action'), result.get('table'),
                   result.get('row_id') or result.get('new_id'),
                   json.dumps(before) if before else None,
                   json.dumps(result.get('values')),
                   bool(result.get('committed')), result.get('error')))
        conn.commit(); conn.close()
    except Exception as e:
        print('[cms_write] could not record the change: ' + str(e)[:160])
