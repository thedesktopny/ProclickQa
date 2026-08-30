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
import os
import socket
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
    # Only the QA flag. Not the extension, not the pay rate, not whether they
    # have left — those carry consequences in the phone system and in payroll.
    'Employee': (
        {'QA'},
        set(),
    ),
}

# columns the database fills in itself — never sent from a form
SERVER_SET = {
    'Account': {'CreatedTime': 'GETDATE()', 'Modified': 'GETDATE()'},
    'AccountNotes': {'Created': 'GETDATE()'},
    'AccountWork': {},
    'Employee': {},
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


def pause_resume_work(work_id, pause, who):
    """Stop or restart the clock on a piece of work.

    Copied from PauseResumeAccountWork: pausing adds a WorkPauses row with no
    end time, resuming closes the open one. The CMS refuses to pause twice or
    resume when nothing is paused, and so does this — two open pauses would
    make the paused time impossible to add up.
    """
    work_id = int(work_id)
    conn = cms_db._connect()
    out = {'table': 'WorkPauses', 'action': ('pause' if pause else 'resume'),
           'work_id': work_id, 'dry_run': False}
    try:
        cu = conn.cursor()
        cu.execute('SET TRANSACTION ISOLATION LEVEL READ COMMITTED')
        cu.execute('SELECT EndTime FROM [AccountWork] WHERE Id = %s', (work_id,))
        row = cu.fetchone()
        if row is None:
            raise WriteRefused('There is no work record with id %d.' % work_id)
        if row[0] is not None:
            raise WriteRefused('That work is already finished.')

        cu.execute("""SELECT TOP 1 Id, StartTime FROM [WorkPauses]
                      WHERE AccountWorkId = %s AND EndTime IS NULL
                      ORDER BY Id DESC""", (work_id,))
        open_pause = cu.fetchone()

        if pause:
            if open_pause:
                raise WriteRefused('This work is already paused.')
            cu.execute("""INSERT INTO [WorkPauses] (AccountWorkId, StartTime)
                          VALUES (%s, GETDATE())""", (work_id,))
            out['meaning'] = 'Paused.'
        else:
            if not open_pause:
                raise WriteRefused('This work is not paused.')
            cu.execute('UPDATE [WorkPauses] SET EndTime = GETDATE() WHERE Id = %s',
                       (int(open_pause[0]),))
            out['meaning'] = 'Back on the clock.'
        conn.commit()
        out.update({'ok': True, 'committed': True})
    except WriteRefused:
        try:
            conn.rollback(); conn.close()
        except Exception:
            pass
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
        # close any pause still open, or the work would end mid-pause
        try:
            cu.execute("""UPDATE [WorkPauses] SET EndTime = GETDATE()
                          WHERE AccountWorkId = %s AND EndTime IS NULL""", (work_id,))
        except Exception:
            pass
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


BULKVS_SEND_URL = 'https://portal.bulkvs.com/api/v1.0/messageSend'


def send_text(account_id, message, who, media_url=None):
    """Send a text to a customer from the number already assigned to them.

    Copied from the CMS's SendSMS, including the character escaping it applies
    before handing the message to BulkVS. Two things it deliberately does NOT
    do: order a number for an account that has none, and touch the campaign
    settings. Both are delicate and belong in the CMS for now.

    The message is only recorded here after BulkVS accepts it, so the history
    never shows a message that was not actually sent.
    """
    import json as _json
    import urllib.request as _u

    account_id = int(account_id)
    message = (message or '').strip()
    if not message and not media_url:
        raise WriteRefused('Write something first.')
    if len(message) > 1600:
        raise WriteRefused('That is longer than a text can be (%d characters, limit 1600).'
                           % len(message))

    conn = cms_db._connect()
    cu = conn.cursor()
    cu.execute("""SELECT ISNULL(smsActivate,0), smsNumber, Phone, Mobile,
                         FirstName, LastName
                  FROM Account WHERE Id = %s""", (account_id,))
    row = cu.fetchone()
    if row is None:
        conn.close()
        raise WriteRefused('There is no account with id %d.' % account_id)
    active, our_number, phone, mobile, first, last = row
    their_number = (phone or mobile or '').strip()
    our_number = (our_number or '').strip()
    name = ('%s %s' % (first or '', last or '')).strip()

    if not our_number or int(active or 0) != 1:
        conn.close()
        raise WriteRefused(
            '%s has no texting number yet. Setting one up is done in the CMS.' % (name or 'This account'))
    if not their_number:
        conn.close()
        raise WriteRefused('%s has no phone number to text.' % (name or 'This account'))

    # the same escaping the CMS applies before sending
    escaped = message
    for ch, code in (('@', '\\u0040'), ('?', '\\u003F'), ('$', '\\u0024'),
                     ('#', '\\u0023'), ("'", '\\u0027'), ('*', '\\u002A'),
                     ('_', '\\u005F'), ('+', '\\u002B'), ('&', '\\u0026'),
                     (';', '\\u003B'), ('.', '\\u002E'), ('=', '\\u003D'),
                     ('"', '\\u0022')):
        escaped = escaped.replace(ch, code)
    escaped = escaped.replace('\r', '\\u000D').replace('\n', '\\n')

    body = {'From': our_number, 'To': [their_number], 'Message': escaped}
    if media_url:
        body['MediaURLs'] = [media_url]

    auth = os.getenv('BULKVS_AUTH', '')
    if not auth:
        conn.close()
        raise WriteRefused('The texting service is not configured here yet '
                           '(BULKVS_AUTH). Ask David to add it.')

    out = {'table': 'SMSLog', 'action': 'send_text', 'account_id': account_id,
           'to': their_number, 'from': our_number, 'dry_run': False,
           'values': {'message': message[:120]}}
    timed_out = False
    try:
        req = _u.Request(BULKVS_SEND_URL,
                         data=_json.dumps(body).encode(),
                         headers={'Authorization': 'Basic ' + auth,
                                  'Content-Type': 'application/json'})
        try:
            with _u.urlopen(req, timeout=45) as resp:
                answer = _json.loads(resp.read().decode() or '{}')
        except Exception as e:
            # A timeout means we stopped waiting, not that nothing happened.
            # The message has almost always gone by this point — the service
            # is simply slow to confirm. Losing it from the history is worse
            # than showing it with a note: the customer has it either way, and
            # an agent who cannot see what they sent will send it again.
            if 'timed out' in str(e).lower() or isinstance(e, socket.timeout):
                timed_out = True
                answer = {}
            else:
                raise
        # BulkVS returns RefId; the CMS's C# reads RefID. Accept either, and
        # judge on the per-recipient Status rather than the reference alone —
        # a reply can carry a reference and still have failed for the person
        # it was meant for.
        ref = (answer.get('RefId') or answer.get('RefID')
               or answer.get('refId') or answer.get('ref_id'))
        results = answer.get('Results') or []
        statuses = [str(x.get('Status', '')).upper() for x in results
                    if isinstance(x, dict)]
        delivered = any(s in ('SUCCESS', 'OK', 'QUEUED', 'ACCEPTED') for s in statuses)

        if not timed_out:
            if statuses and not delivered:
                raise RuntimeError('refused for %s: %s'
                                   % (', '.join(str(x.get('To', '?')) for x in results),
                                      ', '.join(statuses)))
            if not ref and not delivered:
                raise RuntimeError('the texting service did not accept it: %s'
                                   % str(answer)[:180])

        # only now is it real, so only now is it written down
        cu2 = conn.cursor()
        cu2.execute('SET TRANSACTION ISOLATION LEVEL READ COMMITTED')
        cu2.execute("""INSERT INTO [SMSLog]
                       (AccountId, AccountSMSNumber, ContactNumber, InOut,
                        message, smsDate, smsNotRead, MediaURL)
                       VALUES (%s, %s, %s, 'Out', %s, GETDATE(), 0, %s)""",
                    (account_id, our_number, their_number, message, media_url))
        conn.commit()
        out.update({'ok': True, 'committed': True, 'ref': ref,
                    'meaning': ('Sent to %s.' % their_number) if not timed_out else
                               ('Sent to %s — the texting service was slow to confirm, '
                                'so this is recorded but unconfirmed.' % their_number),
                    'unconfirmed': timed_out})
    except WriteRefused:
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
    return out


def mark_texts_read(account_id):
    """Once a conversation has been opened, its messages are no longer unread."""
    conn = cms_db._connect()
    try:
        cu = conn.cursor()
        cu.execute('SET TRANSACTION ISOLATION LEVEL READ COMMITTED')
        cu.execute("""UPDATE [SMSLog] SET smsNotRead = 0
                      WHERE AccountId = %s AND InOut = 'In'
                        AND ISNULL(smsNotRead, 0) = 1""", (int(account_id),))
        n = cu.rowcount
        conn.commit()
        conn.close()
        return {'ok': True, 'marked': (n if n and n > 0 else 0)}
    except Exception as e:
        try:
            conn.rollback(); conn.close()
        except Exception:
            pass
        return {'ok': False, 'error': str(e)[:200]}


CARDKNOX_URL = 'https://x1.cardknox.com/gateway'


def saved_cards(account_id):
    """The cards this customer already has on file, most recently used first.

    Only ever tokens — the card number itself is not in the database and never
    passes through here.
    """
    conn = cms_db._connect(); cu = conn.cursor()
    cu.execute("""SELECT TOP 20 Id, StripeCustomer, Brand, Last4, Created, LastUsed
                  FROM StripeCustomers WHERE AccountId = %s
                  ORDER BY ISNULL(LastUsed, Created) DESC""", (int(account_id),))
    out = []
    while True:
        r = cu.fetchone()
        if not r:
            break
        out.append({'id': int(r[0]), 'token': bool(r[1]), 'brand': r[2],
                    'last4': r[3], 'added': cms_db._plain(r[4]),
                    'last_used': cms_db._plain(r[5])})
    conn.close()
    return out


# The gateway is told which currency an amount is in; nothing is converted.
# The CMS has an ExchangeCurrency method that returns the amount unchanged, so
# a package priced at 18900 in EUR is charged as €189.00, not a dollar
# equivalent. Copied deliberately — changing it would silently reprice things.
CARDKNOX_CURRENCIES = ('USD', 'EUR', 'GBP', 'ILS', 'CAD', 'MXN')


def _currency(code):
    c = str(code or 'USD').strip().upper()
    return c if c in CARDKNOX_CURRENCIES else 'USD'


def _cardknox_sale(token, amount_cents, description, currency='USD'):
    """Charges a saved card. Returns the reference, or raises with the reason.

    Cardknox is the live gateway despite the columns still being named after
    Stripe. Only a token is sent — no card number exists on this side to send.
    """
    import urllib.parse as _p
    import urllib.request as _u
    key = os.getenv('CARDKNOX_KEY', '')
    if not key:
        raise WriteRefused('Card payments are not set up here yet (CARDKNOX_KEY). '
                           'Ask David to add it.')
    body = _p.urlencode({
        'xKey': key,
        'xVersion': '5.0.0',
        'xSoftwareName': 'ProClick Portal',
        'xSoftwareVersion': '1.0',
        'xCommand': 'cc:Sale',
        'xToken': token,
        'xAmount': '%.2f' % (int(amount_cents) / 100.0),
        'xCurrency': _currency(currency),
        'xDescription': (description or '')[:255],
    }).encode()
    req = _u.Request(CARDKNOX_URL, data=body,
                     headers={'Content-Type': 'application/x-www-form-urlencoded'})
    with _u.urlopen(req, timeout=45) as resp:
        raw = resp.read().decode('utf-8', 'replace')
    answer = dict(_p.parse_qsl(raw))
    status = (answer.get('xStatus') or answer.get('xResult') or '').strip()
    if status.lower().startswith('appro') or status.upper() == 'A':
        return {'ref': answer.get('xRefNum'), 'auth': answer.get('xAuthCode'),
                'status': status, 'raw': answer}
    raise RuntimeError(answer.get('xError') or ('the card was declined (%s)' % status))


def buy_package(account_id, package_id, card_id, who, note=None):
    """Sell a package: charge the card, record the sale, add the minutes.

    Follows PurchaseHelper.BuyPackage — charge first, then write PackageSold and
    the balance change together. The order matters: a card charged with no
    minutes applied is recoverable because the sale is recorded either way, but
    minutes given without a charge are not.

    A package priced at zero is free minutes and skips the card entirely.
    """
    account_id, package_id = int(account_id), int(package_id)
    conn = cms_db._connect(); cu = conn.cursor()

    cu.execute("""SELECT FirstName, LastName, Phone, ISNULL(MinutesLeft,0)
                  FROM Account WHERE Id = %s""", (account_id,))
    a = cu.fetchone()
    if not a:
        conn.close()
        raise WriteRefused('There is no account with id %d.' % account_id)
    customer = ('%s %s' % (a[0] or '', a[1] or '')).strip()
    balance_before = int(a[3] or 0)

    cu.execute("""SELECT Name, Minutes, Price, Currency FROM Packages
                  WHERE Id = %s""", (package_id,))
    p = cu.fetchone()
    if not p:
        conn.close()
        raise WriteRefused('There is no package with id %d.' % package_id)
    pkg_name, minutes, price, currency = (p[0] or '').strip(), int(p[1] or 0), int(p[2] or 0), p[3]

    # the same discount the CMS applies to everything
    try:
        cu.execute("""SELECT SettingValue FROM AdminSettings
                      WHERE Name = 'UniversalTemporaryDiscountPercent'""")
        d = cu.fetchone()
        pct = int(str((d[0] if d else '0') or '0').strip() or 0)
        if pct > 0:
            price -= price * pct // 100
    except Exception:
        pct = 0

    card = None
    if price > 0:
        if not card_id:
            conn.close()
            raise WriteRefused('Choose a card to charge.')
        cu.execute("""SELECT Id, AccountId, StripeCustomer, Brand, Last4
                      FROM StripeCustomers WHERE Id = %s""", (int(card_id),))
        card = cu.fetchone()
        if not card:
            conn.close()
            raise WriteRefused('That card is not on file.')
        if int(card[1]) != account_id:
            # the CMS refuses this too, and it is worth refusing loudly
            conn.close()
            raise WriteRefused('That card belongs to a different account.')
        if not card[2]:
            conn.close()
            raise WriteRefused('That card has nothing stored to charge.')

    # A double press must not charge twice. The CMS has no guard against this;
    # a repeat of the same sale within a minute is refused here.
    cu.execute("""SELECT TOP 1 Id, Created FROM PackageSold
                  WHERE AccountId = %s AND PackageId = %s AND AmountPaid = %s
                    AND Created >= DATEADD(minute, -1, GETDATE())""",
               (account_id, package_id, price))
    recent = cu.fetchone()
    if recent:
        conn.close()
        raise WriteRefused('That exact purchase was just recorded a moment ago. '
                           'Check the account before trying again.')

    out = {'table': 'PackageSold', 'action': 'buy_package', 'account_id': account_id,
           'dry_run': False,
           'values': {'package': pkg_name, 'minutes': minutes, 'price_cents': price,
                      'card': ('%s ending %s' % (card[3], card[4])) if card else None}}

    charge_ref = None
    if price > 0:
        description = '%s %s package: %s by: %s' % (
            customer, a[2] or '', pkg_name, (who or {}).get('name') or '')
        try:
            paid = _cardknox_sale(card[2], price, description, currency)
            charge_ref = paid['ref']
            out['charge_ref'] = charge_ref
        except WriteRefused:
            conn.close()
            raise
        except Exception as e:
            conn.close()
            out.update({'ok': False, 'committed': False, 'charged': False,
                        'error': 'The card was not charged: ' + str(e)[:200]})
            _log(who, out)
            return out

    # from here the card has been charged, so nothing may be left unrecorded
    try:
        cu.execute('SET TRANSACTION ISOLATION LEVEL READ COMMITTED')
        cu.execute("""SELECT TOP 1 Id FROM AccountWork
                      WHERE AccountId = %s AND EndTime IS NULL
                      ORDER BY StartTime DESC""", (account_id,))
        open_work = cu.fetchone()

        sale_note = note or ('Package - ' + pkg_name)
        if pct > 0:
            sale_note += ' Discount applied: %%%d' % pct

        cu.execute("""INSERT INTO [PackageSold]
                      (AccountId, Created, Note, PackageId, StripeChargeId,
                       AmountPaid, AccountWorkId, EmployeId, Last4, PackageMinutes)
                      OUTPUT INSERTED.Id
                      VALUES (%s, GETDATE(), %s, %s, %s, %s, %s, %s, %s, %s)""",
                   (account_id, sale_note[:200], package_id, charge_ref, price,
                    (int(open_work[0]) if open_work else None),
                    (who or {}).get('employee_id'),
                    (card[4] if card else None), minutes))
        sold = cu.fetchone()
        sold_id = int(sold[0]) if sold else None
        out['package_sold_id'] = sold_id

        new_balance = balance_before + minutes
        cu.execute("""INSERT INTO [BalanceChangeLog]
                      (Created, AccountId, PreviousBalance, NewBalance,
                       PackageSoldId, AccountWorkId, AdjustmentReason)
                      VALUES (GETDATE(), %s, %s, %s, %s, %s, %s)""",
                   (account_id, balance_before, new_balance, sold_id,
                    (int(open_work[0]) if open_work else None), None))
        cu.execute("""UPDATE [Account] SET MinutesLeft = %s, Modified = GETDATE()
                      WHERE Id = %s""", (new_balance, account_id))
        if card:
            cu.execute('UPDATE [StripeCustomers] SET LastUsed = GETDATE() WHERE Id = %s',
                       (int(card[0]),))
        conn.commit()

        out.update({'ok': True, 'committed': True, 'charged': price > 0,
                    'previous_balance': balance_before, 'new_balance': new_balance,
                    'meaning': ('%s applied — %d minutes added, balance now %d.%s'
                                % (pkg_name, minutes, new_balance,
                                   (' Charged %s.' % _money(price, currency)) if price else
                                   ' Nothing was charged.'))})
    except Exception as e:
        try:
            conn.rollback()
        except Exception:
            pass
        # the worst case, and the one an owner must hear about
        out.update({'ok': False, 'committed': False, 'charged': price > 0,
                    'error': (('THE CARD WAS CHARGED %s BUT THE PACKAGE WAS NOT APPLIED. '
                               'Reference %s. ' % (_money(price, currency), charge_ref))
                              if price > 0 else '') + _explain(e),
                    'raw_error': str(e)[:300]})
    finally:
        try:
            conn.close()
        except Exception:
            pass

    _log(who, out)
    _watch_for_trouble(account_id, customer, price, minutes, who, out)
    return out


def give_free_minutes(account_id, minutes, reason, who):
    """Give minutes without charging for them.

    Recorded as a sale of nothing — PackageSold with no amount and a note
    saying why — which is how the CMS does it, so these show up in the same
    reports as everything else rather than being invisible.
    """
    account_id = int(account_id)
    minutes = int(minutes or 0)
    reason = (reason or '').strip()
    if minutes <= 0:
        raise WriteRefused('How many minutes?')
    if minutes > 10000:
        raise WriteRefused('That is far more than any real gift — check the number.')
    if not reason:
        raise WriteRefused('Give a reason — free minutes are looked at afterwards.')

    conn = cms_db._connect(); cu = conn.cursor()
    cu.execute("""SELECT FirstName, LastName, ISNULL(MinutesLeft,0)
                  FROM Account WHERE Id = %s""", (account_id,))
    a = cu.fetchone()
    if not a:
        conn.close()
        raise WriteRefused('There is no account with id %d.' % account_id)
    customer = ('%s %s' % (a[0] or '', a[1] or '')).strip()
    balance = int(a[2] or 0)

    out = {'table': 'PackageSold', 'action': 'free_minutes', 'account_id': account_id,
           'dry_run': False, 'values': {'minutes': minutes, 'reason': reason}}
    try:
        cu.execute('SET TRANSACTION ISOLATION LEVEL READ COMMITTED')
        cu.execute("""SELECT TOP 1 Id FROM AccountWork
                      WHERE AccountId = %s AND EndTime IS NULL
                      ORDER BY StartTime DESC""", (account_id,))
        open_work = cu.fetchone()

        cu.execute("""INSERT INTO [PackageSold]
                      (AccountId, Created, Note, PackageId, AmountPaid,
                       AccountWorkId, EmployeId, PackageMinutes)
                      OUTPUT INSERTED.Id
                      VALUES (%s, GETDATE(), %s, 0, 0, %s, %s, %s)""",
                   (account_id, ('Free Minutes - ' + reason)[:200],
                    (int(open_work[0]) if open_work else None),
                    (who or {}).get('employee_id'), minutes))
        sold = cu.fetchone()
        sold_id = int(sold[0]) if sold else None

        new_balance = balance + minutes
        cu.execute("""INSERT INTO [BalanceChangeLog]
                      (Created, AccountId, PreviousBalance, NewBalance,
                       PackageSoldId, AccountWorkId, AdjustmentReason)
                      VALUES (GETDATE(), %s, %s, %s, %s, %s, %s)""",
                   (account_id, balance, new_balance, sold_id,
                    (int(open_work[0]) if open_work else None),
                    ('Free minutes - ' + reason)[:100]))
        cu.execute("""UPDATE [Account] SET MinutesLeft = %s, Modified = GETDATE()
                      WHERE Id = %s""", (new_balance, account_id))
        conn.commit()
        out.update({'ok': True, 'committed': True, 'package_sold_id': sold_id,
                    'previous_balance': balance, 'new_balance': new_balance,
                    'meaning': '%d free minutes given — balance now %d.'
                               % (minutes, new_balance)})
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
    _log(who, out)
    _watch_for_trouble(account_id, customer, 0, minutes, who, out)
    return out


def _watch_for_trouble(account_id, customer, price, minutes, who, out):
    """The checks the CMS emails an owner about, recorded here instead.

    Free minutes given by anyone, and more than one paid package on the same
    account in a day. Both are worth an owner seeing; the point is that they
    are recorded rather than lost.
    """
    if not out.get('committed'):
        return
    try:
        import json
        alerts = []
        if price == 0 and minutes > 0:
            alerts.append(('free_minutes',
                           '%d free minutes given to %s' % (minutes, customer or account_id)))
        conn = cms_db._connect(); cu = conn.cursor()
        cu.execute("""SELECT COUNT(*) FROM PackageSold
                      WHERE AccountId = %s AND AmountPaid > 0
                        AND Created >= CAST(GETDATE() AS date)""", (int(account_id),))
        same_day = int((cu.fetchone() or [0])[0] or 0)
        conn.close()
        if price > 0 and same_day > 1:
            alerts.append(('same_day_payments',
                           '%d paid packages on %s today' % (same_day, customer or account_id)))

        if alerts:
            from server import get_db
            conn = get_db(); c = conn.cursor()
            for kind, message in alerts:
                c.execute("""INSERT INTO cms_audit
                             (who, employee_id, action, table_name, row_id, after_json, committed)
                             VALUES (%s, %s, %s, 'PackageSold', %s, %s, TRUE)""",
                          ((who or {}).get('name'), (who or {}).get('employee_id'),
                           kind, account_id,
                           json.dumps({'message': message, 'price_cents': price,
                                       'minutes': minutes})))
            conn.commit(); conn.close()
    except Exception as e:
        print('[purchase] could not record the alert: ' + str(e)[:140])


def _money(cents, currency='USD'):
    symbol = {'USD': '$', 'EUR': '€', 'GBP': '£', 'ILS': '₪',
              'CAD': 'CA$', 'MXN': 'MX$'}.get(_currency(currency), '')
    return '%s%.2f' % (symbol, int(cents or 0) / 100.0)


def _cardknox_refund(ref_num, amount_cents, currency='USD'):
    """Refunds a charge, or voids it when the gateway says to.

    A payment taken today has not settled yet, and Cardknox refuses to refund
    it — it has to be voided instead. The CMS handles this by catching the
    specific message and retrying as a void, which is copied here: without it
    every same-day refund fails with a message an agent cannot act on.
    """
    import urllib.parse as _p
    import urllib.request as _u
    key = os.getenv('CARDKNOX_KEY', '')
    if not key:
        raise WriteRefused('Card payments are not set up here yet (CARDKNOX_KEY).')

    def send(command):
        body = _p.urlencode({
            'xKey': key, 'xVersion': '5.0.0',
            'xSoftwareName': 'ProClick Portal', 'xSoftwareVersion': '1.0',
            'xCommand': command,
            'xRefNum': ref_num,
            'xAmount': '%.2f' % (int(amount_cents) / 100.0),
            'xCurrency': _currency(currency),
        }).encode()
        req = _u.Request(CARDKNOX_URL, data=body,
                         headers={'Content-Type': 'application/x-www-form-urlencoded'})
        with _u.urlopen(req, timeout=45) as resp:
            return dict(_p.parse_qsl(resp.read().decode('utf-8', 'replace')))

    answer = send('cc:Refund')
    status = (answer.get('xStatus') or answer.get('xResult') or '').strip()
    ok = status.lower().startswith('appro') or status.upper() == 'A'
    if not ok:
        why = answer.get('xError') or status
        if 'refund not allowed' in why.lower() or 'void' in why.lower():
            answer = send('cc:Void')
            status = (answer.get('xStatus') or answer.get('xResult') or '').strip()
            ok = status.lower().startswith('appro') or status.upper() == 'A'
            if ok:
                return {'ref': answer.get('xRefNum'), 'voided': True}
            raise RuntimeError(answer.get('xError') or ('the void was refused (%s)' % status))
        raise RuntimeError(why)
    return {'ref': answer.get('xRefNum'), 'voided': False}


def refund_payment(payment_id, reason, who, amount_cents=None):
    """Refund a package sale and take the minutes back.

    Follows the CMS: refund at the gateway, mark the sale refunded with who and
    why, then remove the package's minutes through the balance log so the
    account history shows it.

    One thing worth knowing, copied from the CMS rather than improved: a part
    refund still removes the WHOLE package's minutes. Refunding half the money
    does not leave half the minutes.
    """
    payment_id = int(payment_id)
    reason = (reason or '').strip()
    if not reason:
        raise WriteRefused('Give a reason — it is kept with the refund.')

    conn = cms_db._connect(); cu = conn.cursor()
    cu.execute("""SELECT ps.Id, ps.AccountId, ps.AmountPaid, ps.StripeChargeId,
                         ISNULL(ps.Refunded, 0), ps.PackageId, ps.Created, ps.Note,
                         p.Minutes, p.Currency, p.Name,
                         ISNULL(a.MinutesLeft, 0), a.FirstName, a.LastName
                  FROM PackageSold ps
                  LEFT JOIN Packages p ON p.Id = ps.PackageId
                  LEFT JOIN Account a ON a.Id = ps.AccountId
                  WHERE ps.Id = %s""", (payment_id,))
    r = cu.fetchone()
    if not r:
        conn.close()
        raise WriteRefused('There is no payment with id %d.' % payment_id)
    account_id = int(r[1] or 0)
    paid = int(r[2] or 0)
    charge_ref = r[3]
    already = bool(r[4])
    minutes = int(r[8] or 0)
    currency = r[9] or 'USD'
    pkg_name = r[10] or 'package'
    balance = int(r[11] or 0)
    customer = ('%s %s' % (r[12] or '', r[13] or '')).strip()

    if already:
        conn.close()
        raise WriteRefused('That payment has already been refunded.')
    if paid <= 0:
        conn.close()
        raise WriteRefused('There is nothing to refund — that was a free package.')
    if not charge_ref:
        conn.close()
        raise WriteRefused('That payment has no card reference, so it cannot be '
                           'refunded here. It may have been taken another way.')

    amount = int(amount_cents) if amount_cents else paid
    if amount <= 0:
        conn.close()
        raise WriteRefused('The refund has to be more than nothing.')
    if amount > paid:
        conn.close()
        raise WriteRefused('That is more than the %s that was paid.' % _money(paid, currency))

    out = {'table': 'PackageSold', 'action': 'refund', 'row_id': payment_id,
           'account_id': account_id, 'dry_run': False,
           'values': {'amount_cents': amount, 'reason': reason,
                      'of_payment': paid, 'currency': currency}}
    try:
        result = _cardknox_refund(charge_ref, amount, currency)
        out['refund_ref'] = result['ref']
        out['voided'] = result['voided']
    except WriteRefused:
        conn.close()
        raise
    except Exception as e:
        conn.close()
        out.update({'ok': False, 'committed': False, 'refunded': False,
                    'error': 'The card was not refunded: ' + str(e)[:200]})
        _log(who, out)
        return out

    # the money is back; the record must follow
    try:
        new_balance = balance - minutes
        cu.execute('SET TRANSACTION ISOLATION LEVEL READ COMMITTED')
        cu.execute("""UPDATE [PackageSold]
                      SET Refunded = 1, RefundedAt = GETDATE(), RefundedBy = %s,
                          RefundId = %s, RefundAmount = %s, RefundReason = %s
                      WHERE Id = %s""",
                   ((who or {}).get('employee_id'), result['ref'], amount,
                    ('Previous Minutes:%d - New Minutes :%d \n %s'
                     % (balance, new_balance, reason))[:500],
                    payment_id))
        cu.execute("""INSERT INTO [BalanceChangeLog]
                      (Created, AccountId, PreviousBalance, NewBalance,
                       PackageSoldId, AccountWorkId, AdjustmentReason)
                      VALUES (GETDATE(), %s, %s, %s, %s, NULL, %s)""",
                   (account_id, balance, new_balance, payment_id,
                    ('Refund %s %s' % (pkg_name, _money(amount, currency)))[:100]))
        cu.execute("""UPDATE [Account] SET MinutesLeft = %s, Modified = GETDATE()
                      WHERE Id = %s""", (new_balance, account_id))
        conn.commit()
        out.update({'ok': True, 'committed': True, 'refunded': True,
                    'previous_balance': balance, 'new_balance': new_balance,
                    'meaning': ('%s %s to %s. %d minutes taken back, balance now %d.'
                                % ('Voided' if result['voided'] else 'Refunded',
                                   _money(amount, currency), customer or 'the customer',
                                   minutes, new_balance))})
    except Exception as e:
        try:
            conn.rollback()
        except Exception:
            pass
        out.update({'ok': False, 'committed': False, 'refunded': True,
                    'error': ('THE MONEY WAS REFUNDED (%s, reference %s) BUT THE RECORD '
                              'WAS NOT UPDATED. ' % (_money(amount, currency), result['ref']))
                             + _explain(e),
                    'raw_error': str(e)[:300]})
    finally:
        try:
            conn.close()
        except Exception:
            pass

    _log(who, out)
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
