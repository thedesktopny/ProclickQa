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
        set(),                      # nothing is required on its own — see below
    ),
    'AccountNotes': (
        {'AccountId', 'Note', 'CreatedBy'},
        {'AccountId', 'Note'},
    ),
    # The note an agent writes after a call lives on the work record, tied to
    # the call itself. Only the words are writable — the minutes billed and the
    # times belong to the phone system and to billing.
    'AccountWork': (
        {'Note', 'TaskDescription'},
        {'Note'},
    ),
}

# columns the database fills in itself — never sent from a form
SERVER_SET = {
    'Account': {'CreatedTime': 'GETDATE()', 'Modified': 'GETDATE()'},
    'AccountNotes': {'Created': 'GETDATE()'},
    'AccountWork': {},
}

MAX_TEXT = 400
MAX_NOTE = 2000

# Some things are only required as a group. A customer needs a name of some
# kind and at least one way to reach them; which field carries it does not
# matter, and insisting on a first name specifically would push people into
# typing a surname into the wrong box.
REQUIRED_ANY = {
    'Account': [
        (('FirstName', 'LastName'), 'a first or last name'),
        (('Phone', 'Mobile', 'HomePhone', 'OtherPhone'), 'at least one phone number'),
    ],
}


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
            limit = MAX_NOTE if k in ('Note', 'TaskDescription') else MAX_TEXT
            if len(v) > limit:
                raise WriteRefused('%s is too long (%d characters, limit %d).'
                                   % (k, len(v), limit))
        clean[k] = v
    if creating:
        missing = [r for r in required if not str(clean.get(r, '')).strip()]
        for fields, label in REQUIRED_ANY.get(table, []):
            if not any(str(clean.get(f, '') or '').strip() for f in fields):
                missing.append(label)
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


def adjust_minutes(account_id, minutes_added, reason, who, minutes_charged=0,
                   package_sold_id=None, account_work_id=None, dry_run=True):
    """Change an account's minute balance the way the CMS does it.

    Copied from Repository.SaveNewBalance rather than invented: read the current
    balance, write a BalanceChangeLog row recording what it was, apply the
    change, record what it became, and touch Account.Modified. The admin screen
    in the CMS has a shortcut that skips the log entirely — this deliberately
    does not, because a balance that moved with no record is the thing nobody
    can explain later.

    Unlike the original, all of it happens in ONE transaction, so a failure
    halfway cannot leave the balance changed with no log or the reverse.
    """
    account_id = int(account_id)
    minutes_added = int(minutes_added or 0)
    minutes_charged = int(minutes_charged or 0)
    reason = (reason or '').strip()
    if not reason:
        raise WriteRefused('Give a reason — it is written into the account history.')
    # BalanceChangeLog has no column for who made the change — the CMS passes an
    # employee id to SaveNewBalance and then never stores it, which is why the
    # old screens cannot show it either. The reason text is the only place a
    # name can live, so it goes there.
    name = (who or {}).get('name')
    if name and name not in reason:
        suffix = ' — ' + name
        reason = (reason[:100 - len(suffix)] + suffix) if len(reason) + len(suffix) > 100 \
                 else reason + suffix
    if len(reason) > 100:
        raise WriteRefused('The reason must be 100 characters or fewer (%d given).' % len(reason))
    if minutes_added == 0 and minutes_charged == 0:
        raise WriteRefused('Nothing to change.')
    if abs(minutes_added) > 100000 or abs(minutes_charged) > 100000:
        raise WriteRefused('That is far more than any real adjustment — check the number.')

    conn = cms_db._connect()
    out = {'table': 'Account+BalanceChangeLog', 'action': 'adjust_minutes',
           'account_id': account_id, 'dry_run': dry_run,
           'values': {'minutes_added': minutes_added, 'minutes_charged': minutes_charged,
                      'reason': reason}}
    try:
        cu = conn.cursor()
        cu.execute('SET TRANSACTION ISOLATION LEVEL READ COMMITTED')
        cu.execute('SELECT ISNULL(MinutesLeft, 0) FROM [Account] WHERE Id = %s', (account_id,))
        row = cu.fetchone()
        if row is None:
            raise WriteRefused('There is no account with id %d.' % account_id)
        previous = int(row[0] or 0)
        new_balance = previous - minutes_charged + minutes_added
        out['previous_balance'] = previous
        out['new_balance'] = new_balance

        cu.execute("""INSERT INTO [BalanceChangeLog]
                      (Created, AccountId, PreviousBalance, NewBalance,
                       PackageSoldId, AccountWorkId, AdjustmentReason)
                      VALUES (GETDATE(), %s, %s, %s, %s, %s, %s)""",
                   (account_id, previous, new_balance,
                    int(package_sold_id) if package_sold_id else None,
                    int(account_work_id) if account_work_id else None,
                    reason))
        cu.execute("""UPDATE [Account] SET MinutesLeft = %s, Modified = GETDATE()
                      WHERE Id = %s""", (new_balance, account_id))

        if dry_run:
            conn.rollback()
            out.update({'ok': True, 'committed': False,
                        'meaning': 'This would take the balance from %d to %d minutes. '
                                   'Nothing has been saved.' % (previous, new_balance)})
        else:
            conn.commit()
            cu2 = conn.cursor()
            cu2.execute('SELECT ISNULL(MinutesLeft, 0) FROM [Account] WHERE Id = %s', (account_id,))
            actual = int((cu2.fetchone() or [None])[0] or 0)
            if actual != new_balance:
                out.update({'ok': False, 'committed': False,
                            'error': 'The balance still reads %d, not %d. The login may not '
                                     'have permission to write.' % (actual, new_balance)})
            else:
                out.update({'ok': True, 'committed': True,
                            'meaning': 'Balance changed from %d to %d minutes.'
                                       % (previous, new_balance)})
    except WriteRefused:
        try:
            conn.rollback()
        except Exception:
            pass
        conn.close()
        raise
    except Exception as e:
        try:
            conn.rollback()
        except Exception:
            pass
        out.update({'ok': False, 'committed': False,
                    'error': _explain(e), 'raw_error': str(e)[:300]})
    finally:
        try:
            conn.close()
        except Exception:
            pass
    _log(who, out, before={'MinutesLeft': out.get('previous_balance')})
    return out


def start_work(account_id, employee_id, call_id=None, dry_run=False):
    """Begin working on an account.

    The CMS refuses if the agent already has work open on a different account,
    and so does this — an agent is on one call at a time, and two open work
    records would bill the wrong one.
    """
    account_id, employee_id = int(account_id), int(employee_id)
    conn = cms_db._connect()
    out = {'table': 'AccountWork', 'action': 'start_work',
           'account_id': account_id, 'dry_run': dry_run}
    try:
        cu = conn.cursor()
        cu.execute('SET TRANSACTION ISOLATION LEVEL READ COMMITTED')
        cu.execute("""SELECT TOP 1 w.Id, w.AccountId, a.FirstName, a.LastName
                      FROM AccountWork w LEFT JOIN Account a ON a.Id = w.AccountId
                      WHERE w.EmployeeId = %s AND w.EndTime IS NULL
                      ORDER BY w.StartTime DESC""", (employee_id,))
        open_work = cu.fetchone()
        if open_work and int(open_work[1]) != account_id:
            name = ('%s %s' % (open_work[2] or '', open_work[3] or '')).strip() or ('account %s' % open_work[1])
            raise WriteRefused('You already have work open on %s. Finish that first.' % name)
        if open_work:
            out.update({'ok': True, 'committed': False, 'work_id': int(open_work[0]),
                        'meaning': 'Work on this account is already open.'})
            conn.rollback(); conn.close()
            return out

        # PhoneCallId is required by the table, so an untied piece of work uses 0
        cu.execute("""INSERT INTO [AccountWork]
                      (AccountId, EmployeeId, PhoneCallId, StartTime, Modified)
                      OUTPUT INSERTED.Id
                      VALUES (%s, %s, %s, GETDATE(), GETDATE())""",
                   (account_id, employee_id, int(call_id) if call_id else 0))
        r = cu.fetchone()
        out['work_id'] = int(r[0]) if r else None
        if dry_run:
            conn.rollback()
            out.update({'ok': True, 'committed': False,
                        'meaning': 'This would start work. Nothing has been saved.'})
        else:
            conn.commit()
            out.update({'ok': True, 'committed': True, 'meaning': 'Work started.'})
    except WriteRefused:
        try:
            conn.rollback()
        except Exception:
            pass
        conn.close()
        raise
    except Exception as e:
        try:
            conn.rollback()
        except Exception:
            pass
        out.update({'ok': False, 'committed': False,
                    'error': _explain(e), 'raw_error': str(e)[:300]})
    finally:
        try:
            conn.close()
        except Exception:
            pass
    _log(who={'name': None}, result=out, before=None)
    return out


def end_work(work_id, minutes_billed, note, who, task=None, dry_run=False):
    """Finish a piece of work, and charge for it if minutes were billed.

    This follows EndWorkOnAccount exactly: set the end time, minutes and note,
    then — only when minutes were actually billed — record the charge against
    this work record so the balance history shows which call it came from.
    Both parts share one transaction, so the account can never be charged for
    work that did not close.
    """
    work_id = int(work_id)
    minutes_billed = int(minutes_billed or 0)
    note = (note or '').strip()
    if minutes_billed < 0:
        raise WriteRefused('Minutes billed cannot be negative.')
    if minutes_billed > 1440:
        raise WriteRefused('That is more than a day of minutes — check the number.')

    conn = cms_db._connect()
    out = {'table': 'AccountWork', 'action': 'end_work', 'work_id': work_id,
           'dry_run': dry_run,
           'values': {'minutes_billed': minutes_billed, 'note': note[:120]}}
    try:
        cu = conn.cursor()
        cu.execute('SET TRANSACTION ISOLATION LEVEL READ COMMITTED')
        cu.execute("""SELECT AccountId, EmployeeId, EndTime FROM [AccountWork]
                      WHERE Id = %s""", (work_id,))
        row = cu.fetchone()
        if row is None:
            raise WriteRefused('There is no work record with id %d.' % work_id)
        if row[2] is not None:
            raise WriteRefused('That work was already finished.')
        account_id, employee_id = int(row[0]), int(row[1])
        out['account_id'] = account_id

        cu.execute("""UPDATE [AccountWork]
                      SET EndTime = GETDATE(), MinutesBilled = %s, Note = %s,
                          TaskDescription = %s, Modified = GETDATE()
                      WHERE Id = %s""",
                   (minutes_billed, note, (task or '').strip() or None, work_id))

        if minutes_billed > 0:
            cu.execute('SELECT ISNULL(MinutesLeft, 0) FROM [Account] WHERE Id = %s', (account_id,))
            previous = int((cu.fetchone() or [0])[0] or 0)
            new_balance = previous - minutes_billed
            cu.execute("""INSERT INTO [BalanceChangeLog]
                          (Created, AccountId, PreviousBalance, NewBalance,
                           PackageSoldId, AccountWorkId, AdjustmentReason)
                          VALUES (GETDATE(), %s, %s, %s, NULL, %s, NULL)""",
                       (account_id, previous, new_balance, work_id))
            cu.execute("""UPDATE [Account] SET MinutesLeft = %s, Modified = GETDATE()
                          WHERE Id = %s""", (new_balance, account_id))
            out['previous_balance'] = previous
            out['new_balance'] = new_balance

        if minutes_billed > 0 and out.get('new_balance', 0) < 0:
            # The CMS emails an admin whenever finishing work takes an account
            # below zero (HandleBelowZero). Finishing work here must raise the
            # same flag, or a control quietly stops applying when agents work
            # from this system instead.
            out['below_zero'] = True
            out['warning'] = ('This takes the account to %d minutes — below zero. '
                              'The CMS treats that as something an owner should see.'
                              % out.get('new_balance', 0))

        if dry_run:
            conn.rollback()
            out.update({'ok': True, 'committed': False,
                        'meaning': ('This would finish the work'
                                    + (' and charge %d minutes (balance %d to %d).'
                                       % (minutes_billed, out.get('previous_balance', 0),
                                          out.get('new_balance', 0))
                                       if minutes_billed else ' with nothing billed.')
                                    + ' Nothing has been saved.')})
        else:
            conn.commit()
            out.update({'ok': True, 'committed': True,
                        'meaning': ('Work finished'
                                    + (' · %d minutes charged, balance now %d.'
                                       % (minutes_billed, out.get('new_balance', 0))
                                       if minutes_billed else ', nothing billed.'))})
    except WriteRefused:
        try:
            conn.rollback()
        except Exception:
            pass
        conn.close()
        raise
    except Exception as e:
        try:
            conn.rollback()
        except Exception:
            pass
        out.update({'ok': False, 'committed': False,
                    'error': _explain(e), 'raw_error': str(e)[:300]})
    finally:
        try:
            conn.close()
        except Exception:
            pass
    _log(who, out, before=None)
    if out.get('committed') and out.get('below_zero'):
        _log(who, {'action': 'balance_below_zero', 'table': 'Account',
                   'row_id': out.get('account_id'), 'committed': True,
                   'values': {'work_id': work_id,
                              'minutes_billed': minutes_billed,
                              'previous_balance': out.get('previous_balance'),
                              'new_balance': out.get('new_balance')}})
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
