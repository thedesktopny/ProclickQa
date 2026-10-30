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
    # Employee details that are safe to change from here. Extension is included
    # because it is how calls reach someone and it does change; the login
    # (AspNetUserId), pay and LeftFirm stay out — those have consequences
    # elsewhere.
    'Employee': (
        {'QA', 'FirstName', 'LastName', 'Extension', 'WithCamera', 'Gender',
         'ExperienceLevel', 'notLogIn', 'ScheduleId', 'PhoneName'},
        set(),
    ),
    # Marking a missed call as dealt with — exactly what the CMS's
    # MarkCallResolved does: Resolved = true and Modified = now. Nothing else
    # on a call row may be touched from here.
    'PhoneCallsLog': (
        {'Resolved', 'ResolvedBy', 'Modified'},
        set(),
    ),
    # Settings tables an admin may edit from the portal. Deliberately narrow:
    # AdminSettings is NOT here, because it holds the live payment key and
    # editing credentials from a web page is a different class of risk.
    # PaymentType has one column besides Id: Type ("Paypal", "UpWork" ...)
    'PaymentType': ({'Type'}, {'Type'}),
    'CallStatusSelection': ({'Name', 'Description', 'Color', 'Active', 'SortOrder'}, set()),
    'Question': ({'QuestionText', 'Active', 'SortOrder'}, set()),
    'TableCompanies': ({'Name', 'PhoneNumber', 'Email', 'Active'}, set()),
    'FeedbackConfig': ({'Name', 'Value', 'Active'}, set()),
    # The refill options customers buy. Price is in cents and Currency decides
    # what is actually charged, so both are checked before saving.
    'Packages': (
        {'Name', 'Minutes', 'Price', 'Currency', 'Commission', 'PhoneSystemOption'},
        {'Minutes', 'Price'},
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
MAX_NOTE = 100000      # the CMS columns are varchar(max); this only stops absurd pastes

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


def send_text(account_id, message, who, media_url=None, to=None):
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
    if to:
        # the old CMS texts whichever number the agent picked — any contact in
        # the conversation, or a new one typed in
        digits = ''.join(ch for ch in str(to) if ch.isdigit())
        if len(digits) == 11 and digits.startswith('1'):
            digits = digits[1:]
        if len(digits) != 10:
            conn.close()
            raise WriteRefused('"%s" is not a ten-digit phone number.' % to)
        their_number = digits
    our_number = (our_number or '').strip()
    name = ('%s %s' % (first or '', last or '')).strip()

    if not our_number or int(active or 0) != 1:
        conn.close()
        raise WriteRefused(
            '%s has no texting number yet. Press "Turn on texting" first.' % (name or 'This account'))
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

    # Sent exactly as the old CMS sends it. Putting the escaped text inside a
    # normal JSON encoder would escape the escapes, and the customer would
    # receive "\u002E" where a full stop should be.
    body = _old_cms_send_body(our_number, their_number,
                              message if message else ' ', media_url)

    auth = os.getenv('BULKVS_AUTH', '')
    if not auth:
        conn.close()
        raise WriteRefused('The texting service is not configured here yet '
                           '(BULKVS_AUTH). Ask David to add it.')

    out = {'table': 'SMSLog', 'action': 'send_text', 'account_id': account_id,
           'to': their_number, 'from': our_number, 'dry_run': False,
           'values': {'message': message[:120]}}
    timed_out = False
    # The old CMS's step before every send: the number must be on one of the
    # two campaigns. Done here too now — without it a number that had been
    # taken off a campaign could not send from this page.
    try:
        plan = campaign_plan(our_number)
        if plan['step'] in ('join', 'swap'):
            apply_campaign_plan(our_number, plan)
        out['campaign'] = {k: plan.get(k) for k in ('step', 'tcr', 'move_out', 'meaning')}
        if plan['step'] in ('missing', 'stuck'):
            out['campaign_warning'] = plan['meaning']
    except Exception as e:
        out['campaign_warning'] = 'Could not check the campaign: ' + str(e)[:160]
    try:
        try:
            answer = _send_via_bulkvs(body, tries=2, wait=6)
            if not isinstance(answer, dict):
                answer = {}
        except BulkVSError as e:
            if e.code != NOT_READY_CODE:
                raise
            # The number has just joined a campaign and BulkVS is not ready
            # for it yet. Keep trying in the background — for up to five
            # minutes — and write it into the history only once it has gone.
            _send_later(body, account_id, our_number, their_number, message, media_url, who)
            conn.close()
            out.update({'ok': True, 'committed': False, 'queued': True,
                        'meaning': ('The number %s was just put on a campaign and BulkVS is still '
                                    'getting it ready. The message will go out automatically within '
                                    'a few minutes and then appear here.' % our_number)})
            _log(who, out, before=None)
            return out
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


def _send_later(body, account_id, our_number, their_number, message, media_url, who):
    import threading

    def run():
        try:
            answer = _send_via_bulkvs(body, tries=20, wait=15)
            c = cms_db._connect()
            try:
                k = c.cursor()
                k.execute('SET TRANSACTION ISOLATION LEVEL READ COMMITTED')
                k.execute("""INSERT INTO [SMSLog]
                             (AccountId, AccountSMSNumber, ContactNumber, InOut,
                              message, smsDate, smsNotRead, MediaURL)
                             VALUES (%s, %s, %s, 'Out', %s, GETDATE(), 0, %s)""",
                          (account_id, our_number, their_number, message, media_url))
                c.commit()
            finally:
                c.close()
            _log(who, {'table': 'SMSLog', 'action': 'send_text_delayed', 'account_id': account_id,
                       'committed': True, 'values': {'to': their_number, 'from': our_number,
                                                     'message': (message or '')[:120]}})
        except Exception as e:
            _log(who, {'table': 'SMSLog', 'action': 'send_text_delayed', 'account_id': account_id,
                       'committed': False, 'error': 'Never sent: ' + str(e)[:200],
                       'values': {'to': their_number, 'from': our_number,
                                  'message': (message or '')[:120]}})
    threading.Thread(target=run, daemon=True).start()


def mark_texts_read(account_id, contact=None):
    """Once a conversation has been opened, its messages are no longer unread —
    just that conversation's, when a contact is given, as the old CMS does."""
    conn = cms_db._connect()
    try:
        cu = conn.cursor()
        cu.execute('SET TRANSACTION ISOLATION LEVEL READ COMMITTED')
        digits = ''.join(ch for ch in str(contact or '') if ch.isdigit())[-10:]
        if len(digits) == 10:
            cu.execute("""UPDATE [SMSLog] SET smsNotRead = 0
                          WHERE AccountId = %s AND InOut = 'In' AND ISNULL(smsNotRead, 0) = 1
                            AND RIGHT(ContactNumber, 10) = %s""", (int(account_id), digits))
        else:
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


def _cardknox_sale(token, amount_cents, description, currency='USD', extra=None):
    """Charges a saved card. Returns the reference, or raises with the reason.

    Cardknox is the live gateway despite the columns still being named after
    Stripe. Only a token is sent — no card number exists on this side to send.
    """
    import urllib.parse as _p
    import urllib.request as _u
    key = os.getenv('CARDKNOX_KEY', '').strip()
    if not key:
        raise WriteRefused('Card payments are not set up here yet (CARDKNOX_KEY). '
                           'Ask David to add it.')
    fields = {
        'xKey': key,
        'xVersion': '5.0.0',
        'xSoftwareName': 'ProClick Portal',
        'xSoftwareVersion': '1.0',
        'xCommand': 'cc:Sale',
        'xToken': token,
        'xAmount': '%.2f' % (int(amount_cents) / 100.0),
        'xCurrency': _currency(currency),
        'xDescription': (description or '')[:255],
    }
    # Cardknox shows these as their own columns, so a transaction can be found
    # by customer or invoice rather than only by reading the description.
    for k, v in (extra or {}).items():
        if v not in (None, ''):
            fields[k] = str(v)[:100]
    body = _p.urlencode(fields).encode()
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


def save_card(account_id, card_token, exp_month, exp_year, who,
              cvv_token=None, cardholder=None, zip_code=None, direct=False):
    """Keep a new card on file for a customer.

    Two ways in, and the first is preferred:

      * a single-use token from Cardknox's hosted fields, where the number went
        from the browser straight to them and never reached this server;
      * the number itself, which is how the CMS has always done it, used when
        no hosted-fields key is configured.

    On the second path the number is used once and discarded — never written to
    the database, never put in a log, never included in an error message, and
    never kept in memory beyond the request. That is the least this can do; it
    is still handling card data, which the first path avoids entirely.

    The security code is optional either way.
    """
    import urllib.parse as _p
    import urllib.request as _u

    account_id = int(account_id)
    card_token = (card_token or '').strip()
    if not card_token:
        raise WriteRefused('The card details did not come through. Try again.')
    try:
        mm = int(str(exp_month).strip())
        yy = int(str(exp_year).strip())
    except Exception:
        raise WriteRefused('Enter the expiry month and year.')
    if not (1 <= mm <= 12):
        raise WriteRefused('The expiry month should be 1 to 12.')
    if yy < 100:
        yy += 2000
    import datetime as _dt
    now = _dt.date.today()
    if yy < now.year or (yy == now.year and mm < now.month):
        raise WriteRefused('That card has already expired.')

    key = os.getenv('CARDKNOX_KEY', '').strip()
    if not key:
        raise WriteRefused('Card payments are not set up here yet (CARDKNOX_KEY).')

    conn = cms_db._connect(); cu = conn.cursor()
    cu.execute("""SELECT FirstName, LastName, Phone FROM Account WHERE Id = %s""",
               (account_id,))
    a = cu.fetchone()
    if not a:
        conn.close()
        raise WriteRefused('There is no account with id %d.' % account_id)
    customer = ('%s %s' % (a[0] or '', a[1] or '')).strip()

    fields = {
        'xKey': key, 'xVersion': '5.0.0',
        'xSoftwareName': 'ProClick Portal', 'xSoftwareVersion': '1.0',
        'xCommand': 'cc:Save',
        # either a single-use token or, on the direct path, the number itself —
        # Cardknox takes both in this field
        'xCardNum': card_token,
        'xExp': '%02d%02d' % (mm, yy % 100),
        'xName': (cardholder or customer or '')[:80],
        'xDescription': ('ProClick customer - %s %s created by %s'
                         % (customer, a[2] or '', (who or {}).get('name') or ''))[:200],
        'xInvoice': 'ACC%d' % account_id,
        'xCustom01': (a[2] or ''),
        'xCustom02': (who or {}).get('name') or '',
    }
    if cvv_token:
        fields['xCVV'] = cvv_token
    if zip_code:
        fields['xZip'] = str(zip_code)[:10]

    out = {'table': 'StripeCustomers', 'action': 'save_card',
           'account_id': account_id, 'dry_run': False,
           # deliberately no card details of any kind — this is written to the
           # audit log, so only the expiry and how it was taken go in
           'values': {'expiry': '%02d/%d' % (mm, yy),
                      'entry': 'typed' if direct else 'hosted fields'}}
    try:
        req = _u.Request(CARDKNOX_URL, data=_p.urlencode(fields).encode(),
                         headers={'Content-Type': 'application/x-www-form-urlencoded'})
        with _u.urlopen(req, timeout=45) as resp:
            answer = dict(_p.parse_qsl(resp.read().decode('utf-8', 'replace')))
        status = (answer.get('xStatus') or answer.get('xResult') or '').strip()
        if not (status.lower().startswith('appro') or status.upper() == 'A'):
            raise RuntimeError(answer.get('xError') or ('the card was not accepted (%s)' % status))
        token = answer.get('xToken')
        if not token:
            raise RuntimeError('no token came back from the card service')

        masked = answer.get('xMaskedCardNumber') or ''
        last4 = (masked[-4:] if len(masked) >= 4 else '') or (answer.get('xCardLast4') or '')
        brand = (answer.get('xCardType') or answer.get('xCardBrand') or 'Card').strip()

        cu.execute('SET TRANSACTION ISOLATION LEVEL READ COMMITTED')
        cu.execute("""INSERT INTO [StripeCustomers]
                      (AccountId, Created, CreatedBy, StripeCustomer, Brand, Last4, LastUsed)
                      OUTPUT INSERTED.Id
                      VALUES (%s, GETDATE(), %s, %s, %s, %s, NULL)""",
                   (account_id, (who or {}).get('employee_id') or 0,
                    token, brand[:50], last4[:4]))
        row = cu.fetchone()
        conn.commit()
        out.update({'ok': True, 'committed': True,
                    'card': {'id': int(row[0]) if row else None,
                             'brand': brand, 'last4': last4},
                    'meaning': '%s ending %s saved.' % (brand, last4 or '????')})
    except WriteRefused:
        raise
    except Exception as e:
        try:
            conn.rollback()
        except Exception:
            pass
        # a gateway error can quote back what was sent, so strip anything
        # resembling a card number before it is logged or shown
        import re as _re
        safe = _re.sub(r'\b\d{12,19}\b', '[card number removed]', str(e))[:300]
        out.update({'ok': False, 'committed': False,
                    'error': _re.sub(r'\b\d{12,19}\b', '[card number removed]',
                                     _explain(e)),
                    'raw_error': safe})
    finally:
        try:
            conn.close()
        except Exception:
            pass
    _log(who, out)
    return out


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
        # the same wording the CMS sends, so the two systems' transactions read
        # alike in the Cardknox portal
        agent_name = (who or {}).get('name') or ''
        description = '%s %s package: %s by: %s' % (
            customer, a[2] or '', pkg_name, agent_name)
        extra = {
            'xName': customer,                  # who it was for
            'xInvoice': 'ACC%d' % account_id,   # findable by account
            'xCustom01': pkg_name,              # what was sold
            'xCustom02': agent_name,            # who sold it
            'xCustom03': '%d minutes' % minutes,
            'xCustom04': (a[2] or ''),          # their phone number
        }
        try:
            paid = _cardknox_sale(card[2], price, description, currency, extra)
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


def _cardknox_refund(ref_num, amount_cents, currency='USD', extra=None):
    """Refunds a charge, or voids it when the gateway says to.

    A payment taken today has not settled yet, and Cardknox refuses to refund
    it — it has to be voided instead. The CMS handles this by catching the
    specific message and retrying as a void, which is copied here: without it
    every same-day refund fails with a message an agent cannot act on.
    """
    import urllib.parse as _p
    import urllib.request as _u
    key = os.getenv('CARDKNOX_KEY', '').strip()
    if not key:
        raise WriteRefused('Card payments are not set up here yet (CARDKNOX_KEY).')

    def send(command):
        fields = {
            'xKey': key, 'xVersion': '5.0.0',
            'xSoftwareName': 'ProClick Portal', 'xSoftwareVersion': '1.0',
            'xCommand': command,
            'xRefNum': ref_num,
            'xAmount': '%.2f' % (int(amount_cents) / 100.0),
            'xCurrency': _currency(currency),
        }
        for k, v in (extra or {}).items():
            if v not in (None, ''):
                fields[k] = str(v)[:100]
        body = _p.urlencode(fields).encode()
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
        result = _cardknox_refund(charge_ref, amount, currency, {
            'xName': customer,
            'xInvoice': 'ACC%d' % account_id,
            'xDescription': ('Refund %s for %s by: %s'
                             % (pkg_name, customer, (who or {}).get('name') or ''))[:255],
            'xCustom01': pkg_name,
            'xCustom02': (who or {}).get('name') or '',
            'xCustom03': reason[:100],
        })
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


def _check_package(values):
    """A package sets what a customer is charged, so the numbers are checked
    rather than trusted. A price of 1890000 instead of 18900 is an $18,900
    charge, and nothing downstream would question it."""
    if values.get('Price') is not None:
        try:
            price = int(values['Price'])
        except Exception:
            raise WriteRefused('The price must be a whole number of cents.')
        if price < 0:
            raise WriteRefused('The price cannot be negative.')
        if price > 500000:
            raise WriteRefused('That price is over $5,000 — it is in CENTS, so '
                               '18900 means $189.00. Check the figure.')
    if values.get('Minutes') is not None:
        try:
            mins = int(values['Minutes'])
        except Exception:
            raise WriteRefused('Minutes must be a whole number.')
        if mins < 0 or mins > 100000:
            raise WriteRefused('That number of minutes does not look right.')
    if values.get('Currency'):
        cur = str(values['Currency']).strip().upper()
        if cur not in CARDKNOX_CURRENCIES:
            raise WriteRefused('The currency must be one the card service accepts: %s.'
                               % ', '.join(CARDKNOX_CURRENCIES))
        values['Currency'] = cur.lower()
    return values


def lock_login(employee_id, reason, who=None):
    """Shut someone out of the CMS and the portal at once.

    Uses ASP.NET Identity's own lockout — LockoutEnabled with an end date far
    in the future — which is exactly what the CMS does when somebody leaves.
    That means the lock holds everywhere they could sign in, not only here.

    Deliberately does NOT delete anything or mark them as having left; a lock
    is reversible and a manager undoes it in a moment.
    """
    conn = cms_db._connect()
    out = {'table': 'AspNetUsers', 'action': 'lock_login', 'dry_run': False,
           'values': {'employee_id': employee_id, 'reason': reason[:200]}}
    try:
        cu = conn.cursor()
        cu.execute('SET TRANSACTION ISOLATION LEVEL READ COMMITTED')
        cu.execute("""SELECT TOP 1 e.AspNetUserId, e.FirstName, e.LastName, u.UserName
                      FROM Employee e
                      LEFT JOIN AspNetUsers u ON u.Id = e.AspNetUserId
                      WHERE e.Id = %s""", (int(employee_id),))
        r = cu.fetchone()
        if not r or not r[0]:
            conn.close()
            raise WriteRefused('That employee has no sign-in to lock.')
        user_id, name = str(r[0]), ('%s %s' % (r[1] or '', r[2] or '')).strip()

        cu.execute("""UPDATE [AspNetUsers]
                      SET LockoutEnabled = 1,
                          LockoutEndDateUtc = '9999-12-30 00:00:00'
                      WHERE Id = %s""", (user_id,))
        conn.commit()

        # read it back — a lock that did not actually save is worse than none,
        # because everyone would believe the person is shut out
        check = conn.cursor()
        check.execute('SELECT LockoutEndDateUtc FROM AspNetUsers WHERE Id = %s', (user_id,))
        back = check.fetchone()
        locked = bool(back and back[0] and str(back[0]).startswith('9999'))
        if not locked:
            raise RuntimeError('the lock did not save — the login is still open')

        out.update({'ok': True, 'committed': True, 'user_id': user_id, 'name': name,
                    'meaning': '%s is locked out of the CMS and the portal.' % name})
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
    _log(who, out)
    return out


def unlock_login(employee_id, who=None):
    """Let somebody back in. Only a manager or admin reaches this."""
    conn = cms_db._connect()
    out = {'table': 'AspNetUsers', 'action': 'unlock_login', 'dry_run': False,
           'values': {'employee_id': employee_id}}
    try:
        cu = conn.cursor()
        cu.execute('SET TRANSACTION ISOLATION LEVEL READ COMMITTED')
        cu.execute("""SELECT TOP 1 AspNetUserId, FirstName, LastName
                      FROM Employee WHERE Id = %s""", (int(employee_id),))
        r = cu.fetchone()
        if not r or not r[0]:
            conn.close()
            raise WriteRefused('That employee has no sign-in.')
        user_id, name = str(r[0]), ('%s %s' % (r[1] or '', r[2] or '')).strip()
        cu.execute('UPDATE [AspNetUsers] SET LockoutEndDateUtc = NULL WHERE Id = %s',
                   (user_id,))
        conn.commit()
        out.update({'ok': True, 'committed': True, 'name': name,
                    'meaning': '%s can sign in again.' % name})
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
    _log(who, out)
    return out


def create_agent(fields, who, dry_run=True):
    """Create a person who can sign in and take calls.

    Three tables have to agree or the result is a half-made account: an
    AspNetUsers row (the login), an AspNetUserRoles row (what they may do) and
    an Employee row (the person, their extension, their pay). They are written
    in ONE transaction, so a failure leaves nothing behind rather than a login
    with no employee or an employee who cannot sign in.

    The password hash is made in ASP.NET Identity's own v3 format and verified
    with the same code the CMS login uses before anything is saved — a hash in
    the wrong shape would create somebody who simply cannot get in, and that is
    not discovered until they try.
    """
    import uuid
    import aspnet_auth

    first = (fields.get('first_name') or '').strip()
    last = (fields.get('last_name') or '').strip()
    email = (fields.get('email') or '').strip()
    extension = (fields.get('extension') or '').strip()
    password = fields.get('password') or ''
    role = (fields.get('role') or '').strip()

    if not first or not last:
        raise WriteRefused('A first and last name are needed.')
    if not email or '@' not in email:
        raise WriteRefused('A working email address is needed — it is the user name '
                           'they sign in with, and where their sign-in code goes.')
    if len(password) < 8:
        raise WriteRefused('The password must be at least 8 characters.')
    if not extension:
        raise WriteRefused('An extension is needed, or calls cannot reach them.')

    # prove the hash works before it is written, not after
    password_hash = aspnet_auth.make_hash_v3(password)
    if not aspnet_auth.verify_password(password, password_hash):
        raise WriteRefused('The password could not be stored safely. Nothing was saved.')

    conn = cms_db._connect()
    cu = conn.cursor()
    cu.execute('SELECT COUNT(*) FROM AspNetUsers WHERE UserName = %s OR Email = %s',
               (email, email))
    if int((cu.fetchone() or [0])[0] or 0) > 0:
        conn.close()
        raise WriteRefused('Someone already signs in with that email address.')
    cu.execute("""SELECT TOP 1 Id, FirstName, LastName FROM Employee
                  WHERE Extension = %s AND ISNULL(LeftFirm, 0) = 0""", (extension,))
    clash = cu.fetchone()
    if clash:
        conn.close()
        raise WriteRefused('Extension %s already belongs to %s %s.'
                           % (extension, clash[1] or '', clash[2] or ''))

    role_id = None
    if role:
        cu.execute('SELECT TOP 1 Id, Name FROM AspNetRoles WHERE Name = %s', (role,))
        r = cu.fetchone()
        if not r:
            conn.close()
            raise WriteRefused('There is no role called "%s" in the CMS.' % role)
        role_id = r[0]

    user_id = str(uuid.uuid4())
    out = {'table': 'Employee', 'action': 'create_agent', 'dry_run': bool(dry_run),
           'values': {'name': '%s %s' % (first, last), 'email': email,
                      'extension': extension, 'role': role or '(none)'}}
    try:
        cu.execute('SET TRANSACTION ISOLATION LEVEL READ COMMITTED')
        cu.execute('BEGIN TRANSACTION')

        cu.execute("""INSERT INTO [AspNetUsers]
                      (Id, UserName, Email, EmailConfirmed, PasswordHash,
                       SecurityStamp, PhoneNumberConfirmed, TwoFactorEnabled,
                       LockoutEnabled, AccessFailedCount)
                      VALUES (%s, %s, %s, 1, %s, %s, 0, 0, 1, 0)""",
                   (user_id, email, email, password_hash, str(uuid.uuid4())))

        if role_id:
            cu.execute('INSERT INTO [AspNetUserRoles] (UserId, RoleId) VALUES (%s, %s)',
                       (user_id, role_id))

        cu.execute("""INSERT INTO [Employee]
                      (FirstName, LastName, Extension, AspNetUserId, LeftFirm,
                       Created, QA, WithCamera, Gender, HourlyRate, PhoneName)
                      OUTPUT INSERTED.Id
                      VALUES (%s, %s, %s, %s, 0, GETDATE(), %s, %s, %s, %s, %s)""",
                   (first, last, extension, user_id,
                    1 if fields.get('is_qa') else 0,
                    1 if fields.get('with_camera') else 0,
                    (fields.get('gender') or None),
                    int(fields.get('hourly_rate') or 0),
                    ((fields.get('phone_name') or '').strip()[:50] or None)))
        row = cu.fetchone()
        employee_id = int(row[0]) if row else None

        if dry_run:
            cu.execute('ROLLBACK TRANSACTION')
            out.update({'ok': True, 'committed': False,
                        'meaning': ('This would create %s %s on extension %s, with a '
                                    'sign-in for %s%s. Nothing has been saved yet.'
                                    % (first, last, extension, email,
                                       (' as %s' % role) if role else ''))})
        else:
            cu.execute('COMMIT TRANSACTION')
            conn.commit()
            # read it back — a commit that silently did nothing has bitten us before
            check = conn.cursor()
            check.execute('SELECT Id, Extension FROM Employee WHERE Id = %s', (employee_id,))
            back = check.fetchone()
            if not back:
                raise RuntimeError('the employee record is not there after saving')
            out.update({'ok': True, 'committed': True, 'employee_id': employee_id,
                        'user_id': user_id,
                        'meaning': '%s %s created on extension %s. They sign in with %s.'
                                   % (first, last, extension, email)})
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
    _log(who, out)
    return out


def set_payment_type(employee_id, payment_type_id, who):
    """Change how one person is paid — Employee.PaymentTypeID and nothing else.

    Kept out of the general Employee whitelist on purpose, so no other form can
    change pay by passing an extra field. Both ids are checked to exist, the
    previous value is logged, and the row is read back before "saved" is said.
    """
    employee_id = int(employee_id); payment_type_id = int(payment_type_id)
    conn = cms_db._connect()
    out = {'table': 'Employee', 'action': 'set_payment_type', 'row_id': employee_id,
           'values': {'PaymentTypeID': payment_type_id}, 'dry_run': False}
    try:
        cu = conn.cursor()
        cu.execute('SELECT Type FROM PaymentType WHERE Id = %s', (payment_type_id,))
        t = cu.fetchone()
        if not t:
            raise WriteRefused('That payment type does not exist.')
        cu.execute('SELECT PaymentTypeID, FirstName, LastName FROM Employee WHERE Id = %s', (employee_id,))
        e = cu.fetchone()
        if not e:
            raise WriteRefused('There is no employee with id %d.' % employee_id)
        out['before'] = {'PaymentTypeID': e[0]}
        cu.execute('SET TRANSACTION ISOLATION LEVEL READ COMMITTED')
        cu.execute('UPDATE Employee SET PaymentTypeID = %s WHERE Id = %s', (payment_type_id, employee_id))
        conn.commit()
        cu = conn.cursor()
        cu.execute('SELECT PaymentTypeID FROM Employee WHERE Id = %s', (employee_id,))
        after = cu.fetchone()
        if not after or after[0] is None or int(after[0]) != payment_type_id:
            out.update({'ok': False, 'committed': False,
                        'error': 'The database accepted the change but the record still reads the same. '
                                 'The login may not have permission to write.'})
        else:
            out.update({'ok': True, 'committed': True, 'payment_type': (t[0] or '').strip(),
                        'meaning': 'Saved and confirmed.'})
    except WriteRefused:
        conn.close()
        raise
    except Exception as ex:
        try:
            conn.rollback()
        except Exception:
            pass
        out.update({'ok': False, 'committed': False, 'error': _explain(ex), 'raw_error': str(ex)[:300]})
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


# ───────────────────────── clock in / break / clock out ─────────────────────
# What the old CMS does on every status change, reproduced exactly:
#   1. a row in EmployeeClocker (Status 1 = in, 2 = on a break, 0 = out, with
#      the declared break minutes and reason on a break), and
#   2. the phone system told, by writing cms_status into the Asterisk MySQL
#      table ps_auths for the agent's extension — that is what decides whether
#      calls ring them. Without step 2 an agent could be "clocked in" here and
#      never receive a call, so clocking is refused until it is set up.
import os as _os

ASTERISK = {
    'host': _os.getenv('ASTERISK_DB_HOST', ''),
    'port': int(_os.getenv('ASTERISK_DB_PORT', '3306') or 3306),
    'user': _os.getenv('ASTERISK_DB_USER', ''),
    'password': _os.getenv('ASTERISK_DB_PASSWORD', ''),
    'database': _os.getenv('ASTERISK_DB_NAME', 'asterisk'),
}
CLOCK_NAMES = {0: 'out', 1: 'in', 2: 'break'}


def asterisk_configured():
    return bool(ASTERISK['host'] and ASTERISK['user'])


def _asterisk_connect():
    import pymysql
    return pymysql.connect(host=ASTERISK['host'], port=ASTERISK['port'], user=ASTERISK['user'],
                           password=ASTERISK['password'], database=ASTERISK['database'],
                           connect_timeout=6, read_timeout=8, write_timeout=8, autocommit=True)


def asterisk_set_status(extension, cms_status):
    """The same statement the old CMS runs on every clock change."""
    conn = _asterisk_connect()
    try:
        cu = conn.cursor()
        cu.execute("UPDATE ps_auths SET cms_status = %s, business_number = '' WHERE id = %s",
                   (int(cms_status), str(extension).strip()))
        return cu.rowcount
    finally:
        conn.close()


def asterisk_check(extension=None):
    """Can we reach the phone system's table at all? Read-only."""
    conn = _asterisk_connect()
    try:
        cu = conn.cursor()
        if extension:
            cu.execute('SELECT id, cms_status, ast_status FROM ps_auths WHERE id = %s', (str(extension),))
        else:
            cu.execute('SELECT id, cms_status, ast_status FROM ps_auths ORDER BY id LIMIT 5')
        return [{'extension': r[0], 'cms_status': r[1], 'ast_status': r[2]} for r in cu.fetchall()]
    finally:
        conn.close()


def _now_local():
    """The CMS stores clock times in New York time, as its own server does."""
    from datetime import datetime
    try:
        from zoneinfo import ZoneInfo
        return datetime.now(ZoneInfo('America/New_York')).replace(tzinfo=None)
    except Exception:
        from datetime import timedelta
        return datetime.utcnow() - timedelta(hours=4)


def _ny_iso(dt):
    """A CMS time (New York, stored without a zone) as a full timestamp with
    its offset, so a browser anywhere — the Philippines included — shows it
    right instead of treating it as its own local time."""
    if dt is None:
        return None
    try:
        from zoneinfo import ZoneInfo
        return dt.replace(tzinfo=ZoneInfo('America/New_York')).isoformat()
    except Exception:
        return cms_db._plain(dt)


def clock_status(employee_id):
    """Where someone stands right now, from their latest EmployeeClocker row —
    and every break taken so far in this shift, so they can see the total."""
    conn = cms_db._connect()
    try:
        cu = conn.cursor()
        cu.execute('SELECT FirstName, LastName, Extension FROM Employee WHERE Id = %s', (int(employee_id),))
        e = cu.fetchone()
        cu.execute("""SELECT TOP 1 Status, Created, BreakMinutes, BreakReason FROM EmployeeClocker
                      WHERE EmployeeId = %s AND Status IS NOT NULL
                      ORDER BY Created DESC, Id DESC""", (int(employee_id),))
        r = cu.fetchone()
        rows, now = [], None
        status = int(r[0]) if r and r[0] is not None else 0
        if status in (1, 2):
            # this shift = everything since the last clock-out (within a day)
            cu.execute("""SELECT Status, Created, BreakMinutes, BreakReason, GETDATE() FROM EmployeeClocker
                          WHERE EmployeeId = %s AND Status IS NOT NULL
                            AND Created >= DATEADD(hour, -20, GETDATE())
                          ORDER BY Created, Id""", (int(employee_id),))
            rows = cu.fetchall()
    finally:
        conn.close()
    shift = []
    if rows:
        now = rows[0][4]
        start = 0
        for i, x in enumerate(rows):
            if int(x[0] or 0) == 0:
                start = i + 1                      # a clock-out ends the earlier shift
        rows = rows[start:]
    breaks = []
    for i, x in enumerate(rows):
        if int(x[0] or 0) != 2:
            continue
        end = rows[i + 1][1] if i + 1 < len(rows) else None
        secs = int(((end or now) - x[1]).total_seconds()) if x[1] else 0
        breaks.append({'start': _ny_iso(x[1]), 'end': _ny_iso(end), 'seconds': max(0, secs),
                       'planned_minutes': int(x[2]) if x[2] is not None else None,
                       'reason': x[3], 'ongoing': end is None})
    shift_start = rows[0][1] if rows else None
    return {'employee_id': int(employee_id),
            'name': ('%s %s' % ((e[0] or '') if e else '', (e[1] or '') if e else '')).strip(),
            'extension': (str(e[2]).strip() if e and e[2] not in (None, '') else None),
            'status': CLOCK_NAMES.get(status, 'out'),
            'since': _ny_iso(r[1]) if r else None,
            'seconds_in_state': (int((now - r[1]).total_seconds()) if (r and now and r[1]) else None),
            'shift_started': _ny_iso(shift_start),
            'break_minutes': (int(r[2]) if r and r[2] is not None else None),
            'break_reason': (r[3] if r else None),
            'breaks': breaks,
            'break_seconds_total': sum(b['seconds'] for b in breaks)}


def clock(employee_id, action, who, minutes=None, reason=None, logged_out_by=None):
    """Start a shift ('in'), go on a break ('break'), come back ('back') or end
    the shift ('out'). Only moves that make sense from where they are now."""
    if not asterisk_configured():
        raise WriteRefused('Clocking here is not switched on yet — the phone system connection '
                           'is not set up. Use the old CMS for now.')
    now = clock_status(employee_id)
    cur = now['status']
    allowed = {'in': ('out',), 'break': ('in',), 'back': ('break',), 'out': ('in', 'break')}
    if action not in allowed:
        raise WriteRefused('Unknown action.')
    if cur not in allowed[action]:
        msg = {'in': ('You are on a break — press Back to work.' if cur == 'break' else 'You are already clocked in.'),
               'break': 'You can only start a break while clocked in.',
               'back': 'You are not on a break.', 'out': 'You are not clocked in.'}[action]
        raise WriteRefused(msg)
    if not now['extension']:
        raise WriteRefused('This person has no extension in the CMS, so the phone system cannot be told.')
    status = {'in': 1, 'back': 1, 'break': 2, 'out': 0}[action]
    mins = None
    if action == 'break':
        try:
            mins = int(minutes)
        except Exception:
            raise WriteRefused('Choose how long the break is.')
        if mins < 1 or mins > 240:
            raise WriteRefused('A break must be between 1 and 240 minutes.')
    reason = (reason or '').strip()[:200] or None
    when = _now_local()

    conn = cms_db._connect()
    out = {'table': 'EmployeeClocker', 'action': 'clock_' + action, 'dry_run': False,
           'values': {'EmployeeId': int(employee_id), 'Status': status, 'BreakMinutes': mins,
                      'BreakReason': reason if action == 'break' else None}}
    if logged_out_by:
        # someone else ended this shift (a team leader or manager); the CMS row
        # is an ordinary clock-out, and who did it and why is kept in our log
        out['values']['logged_out_by'] = logged_out_by
        out['values']['reason'] = reason
        out['values']['at'] = when.isoformat()
        out['action'] = 'clock_out_by_leader'
    try:
        cu = conn.cursor()
        cu.execute('SET TRANSACTION ISOLATION LEVEL READ COMMITTED')
        cu.execute("""INSERT INTO EmployeeClocker (EmployeeId, Created, Status, BreakMinutes, BreakReason)
                      OUTPUT INSERTED.Id VALUES (%s, %s, %s, %s, %s)""",
                   (int(employee_id), when, status, mins, reason if action == 'break' else None))
        r = cu.fetchone()
        conn.commit()
        out['row_id'] = int(r[0]) if r else None
        out['committed'] = True
    except Exception as ex:
        try:
            conn.rollback()
        except Exception:
            pass
        out.update({'ok': False, 'committed': False, 'error': _explain(ex), 'raw_error': str(ex)[:300]})
        _log(who, out)
        return out
    finally:
        try:
            conn.close()
        except Exception:
            pass

    # Do Not Disturb follows the shift: ending the shift ends it, and coming
    # back from a break while it is still on keeps the phone on DND (3)
    phone_status = status
    try:
        d = dnd_state(employee_id)
        if d['on'] and action == 'out':
            c2 = cms_db._connect()
            try:
                _dnd_insert(c2, employee_id, 'END_NOT_DISTRUB', when, 'shift ended', d.get('type'))
                out['dnd_ended'] = True
            finally:
                c2.close()
        elif d['on'] and status == 1:
            phone_status = 3
    except Exception as ex:
        out['dnd_note'] = 'Could not check Do Not Disturb: ' + str(ex)[:120]

    # the clock row is the record; now tell the phone system
    try:
        n = asterisk_set_status(now['extension'], phone_status)
        out['phone_updated'] = bool(n)
        if not n:
            out['phone_warning'] = ('The phone system has no line for extension %s, so it was not updated.'
                                    % now['extension'])
    except Exception as ex:
        out['phone_updated'] = False
        out['phone_warning'] = 'Saved, but the phone system could not be told: ' + str(ex)[:160]
    out['ok'] = True
    out['status'] = CLOCK_NAMES[status]
    out['since'] = _ny_iso(when)
    _log(who, out)
    return out


# ───────────────────────── Do Not Disturb ─────────────────────────────────
# NotDistrubStatus is a log: a NOT_DISTRUB row when someone goes on DND (with
# the reason in Note and the kind in TypeDND), an END_NOT_DISTRUB row when they
# come off. The phone system is told with cms_status 3 while on DND, and back
# to 1 when it ends — the same as the old CMS.

def dnd_state(employee_id):
    """{'on': bool, 'since', 'note', 'type'} from the newest row."""
    conn = cms_db._connect()
    try:
        cu = conn.cursor()
        cu.execute("""SELECT TOP 1 Status, Created, Note, TypeDND FROM NotDistrubStatus
                      WHERE EmployeeID = %s ORDER BY Id DESC""", (int(employee_id),))
        r = cu.fetchone()
    finally:
        conn.close()
    st = str((r[0] if r else '') or '').upper().strip()
    on = bool(st) and not st.startswith('END')
    return {'on': on, 'since': _ny_iso(r[1]) if (r and on) else None,
            'note': (r[2] if (r and on) else None), 'type': (r[3] if (r and on) else None)}


def dnd_types():
    """The kinds of DND people actually use in the CMS, most used first."""
    conn = cms_db._connect()
    try:
        cu = conn.cursor()
        cu.execute("""SELECT TOP 12 TypeDND, COUNT(*) FROM NotDistrubStatus
                      WHERE TypeDND IS NOT NULL AND LTRIM(RTRIM(CAST(TypeDND AS varchar(100)))) <> ''
                        AND Created >= DATEADD(day, -120, GETDATE())
                      GROUP BY TypeDND ORDER BY COUNT(*) DESC""")
        rows = []
        while True:
            r = cu.fetchone()
            if not r:
                break
            rows.append(r)
    finally:
        conn.close()
    return [{'type': str(r[0]).strip(), 'uses': int(r[1] or 0)} for r in rows]


def _dnd_insert(conn, employee_id, status, when, note, kind):
    cu = conn.cursor()
    # refuse rather than guess if the table needs a column we do not fill
    cu.execute("""SELECT COLUMN_NAME FROM INFORMATION_SCHEMA.COLUMNS
                  WHERE TABLE_NAME = 'NotDistrubStatus' AND IS_NULLABLE = 'NO'
                    AND COLUMN_DEFAULT IS NULL
                    AND COLUMNPROPERTY(OBJECT_ID(TABLE_SCHEMA + '.' + TABLE_NAME), COLUMN_NAME, 'IsIdentity') = 0""")
    needed = set()
    while True:
        r = cu.fetchone()
        if not r:
            break
        needed.add(r[0])
    missing = needed - {'EmployeeID', 'Status', 'Created', 'Note', 'TypeDND'}
    if missing:
        raise WriteRefused('The CMS needs more for Do Not Disturb than this page knows how to fill ('
                           + ', '.join(sorted(missing)) + '). Use the old CMS for DND for now.')
    cu.execute('SET TRANSACTION ISOLATION LEVEL READ COMMITTED')
    cu.execute("""INSERT INTO NotDistrubStatus (EmployeeID, Status, Created, Note, TypeDND)
                  OUTPUT INSERTED.Id VALUES (%s, %s, %s, %s, %s)""",
               (int(employee_id), status, when, note, kind))
    r = cu.fetchone()
    conn.commit()
    return int(r[0]) if r else None


def dnd(employee_id, turn_on, who, reason=None, kind=None):
    """Go on Do Not Disturb (a reason is required) or come off it."""
    if not asterisk_configured():
        raise WriteRefused('Do Not Disturb here is not switched on yet — the phone system connection '
                           'is not set up. Use the old CMS for now.')
    clock_now = clock_status(employee_id)
    state = dnd_state(employee_id)
    reason = (reason or '').strip()[:300]
    kind = (kind or '').strip()[:60] or None
    if turn_on:
        if state['on']:
            raise WriteRefused('You are already on Do Not Disturb.')
        if clock_now['status'] != 'in':
            raise WriteRefused('Start your shift first — Do Not Disturb is only while working.')
        if not reason:
            raise WriteRefused('Type the reason for Do Not Disturb.')
    elif not state['on']:
        raise WriteRefused('You are not on Do Not Disturb.')
    if not clock_now['extension']:
        raise WriteRefused('This person has no extension in the CMS, so the phone system cannot be told.')

    when = _now_local()
    out = {'table': 'NotDistrubStatus', 'action': 'dnd_on' if turn_on else 'dnd_off', 'dry_run': False,
           'values': {'EmployeeID': int(employee_id), 'Note': reason or None, 'TypeDND': kind}}
    conn = cms_db._connect()
    try:
        out['row_id'] = _dnd_insert(conn, employee_id, 'NOT_DISTRUB' if turn_on else 'END_NOT_DISTRUB',
                                    when, reason or None, kind if turn_on else state.get('type'))
        out['committed'] = True
    except WriteRefused:
        raise
    except Exception as ex:
        try:
            conn.rollback()
        except Exception:
            pass
        out.update({'ok': False, 'committed': False, 'error': _explain(ex), 'raw_error': str(ex)[:300]})
        _log(who, out)
        return out
    finally:
        try:
            conn.close()
        except Exception:
            pass

    # phone system: 3 while on DND; when it ends, back to whatever the clock says
    phone = 3 if turn_on else {'in': 1, 'break': 2, 'out': 0}.get(clock_now['status'], 1)
    try:
        n = asterisk_set_status(clock_now['extension'], phone)
        out['phone_updated'] = bool(n)
        if not n:
            out['phone_warning'] = 'The phone system has no line for extension %s.' % clock_now['extension']
    except Exception as ex:
        out['phone_updated'] = False
        out['phone_warning'] = 'Saved, but the phone system could not be told: ' + str(ex)[:160]
    out['ok'] = True
    out['dnd'] = bool(turn_on)
    _log(who, out)
    return out


# ═════════════════════════════════════════════════════════════════════════════
# TEXTING — numbers, the two campaigns, and sending
#
# Copied from the old CMS's SMSController, with its rough edges smoothed:
#
#   * A customer's number comes from the spare pool (SMSNumberTable), oldest
#     first, matching their area code. Taking one orders a replacement from
#     BulkVS so the pool refills.
#   * Before every send the number must be on one of the two campaigns. Static
#     numbers (AccountSMS with StaticCompign = 1) are fixed to theirs. Anything
#     else: if BulkVS already has it on a campaign, fine; otherwise it joins
#     the first campaign under 49; if both are full, the number that was put on
#     longest ago is taken off and this one takes its place.
#   * Turning texting off releases the number at BulkVS and clears the account.
#
# Differences from the old code, all on the side of caution: a number is only
# ever taken off a campaign if WE put it there (its Lidb is our timestamp) and
# it is not static; BulkVS's answer is checked to be about the number asked
# for; and the "not ready yet" retry gives up after a minute instead of never.
# ═════════════════════════════════════════════════════════════════════════════

BV_BASE = 'https://portal.bulkvs.com/api/v1.0'
SMS_TRUNK = 'SMS-ProClick'
CAMPAIGN_LIMIT = 48           # David's rule: never more than 48 on a campaign
                              # (BulkVS's own ceiling is 49; one kept spare for safety)
NOT_READY_CODE = '7401'       # BulkVS: the number is not ready to send yet


class BulkVSError(Exception):
    def __init__(self, message, code=None, status=None):
        super().__init__(message)
        self.code = str(code) if code is not None else None
        self.status = status


def bv_configured():
    return bool(os.getenv('BULKVS_AUTH', '').strip())


def _bv(method, path, body=None, timeout=25, raw=None):
    """One call to BulkVS. Raises BulkVSError with their code and description
    when they refuse, so callers can tell 'not ready' from 'no'. 'raw' sends a
    body exactly as given, for the message text (see _old_cms_send_body)."""
    import json as _json
    import urllib.request as _u
    import urllib.error as _ue
    auth = os.getenv('BULKVS_AUTH', '').strip()
    if not auth:
        raise WriteRefused('The texting service is not configured here yet (BULKVS_AUTH).')
    if raw is not None:
        data = raw.encode('utf-8')
    else:
        data = _json.dumps(body).encode() if body is not None else None
    req = _u.Request(BV_BASE + path, data=data, method=method,
                     headers={'Authorization': 'Basic ' + auth,
                              'Content-Type': 'application/json'})
    try:
        with _u.urlopen(req, timeout=timeout) as resp:
            text = resp.read().decode()
    except _ue.HTTPError as e:
        try:
            raw = e.read().decode()
        except Exception:
            raw = ''
        code, desc = None, raw[:200] or str(e)
        try:
            j = _json.loads(raw)
            if isinstance(j, dict):
                code = j.get('code') or j.get('Code')
                desc = j.get('description') or j.get('Description') or desc
        except Exception:
            pass
        raise BulkVSError('BulkVS refused: %s' % desc, code=code, status=e.code)
    try:
        return _json.loads(text) if text.strip() else {}
    except Exception:
        return text


def _ten(n):
    d = ''.join(ch for ch in str(n or '') if ch.isdigit())
    return d[-10:]


def _rec_tn(r):
    return _ten(r.get('TN') or r.get('Tn') or r.get('tn'))


def _rec_tcr(r):
    m = r.get('Messaging') or {}
    return ((m.get('TCR') or m.get('Tcr') or r.get('TCR') or r.get('Tcr') or '') or '').strip()


def _rec_webhook(r):
    m = r.get('Messaging') or {}
    return (m.get('Webhook') or m.get('WebHook') or r.get('Webhook') or r.get('WebHook') or '')


def _rec_lidb(r):
    return str(r.get('Lidb') or r.get('LIDB') or '').strip()


def _lidb_time(lidb):
    """Our own stamp, yyyyMMddHHmm, as the old CMS writes it — or None."""
    import re as _re
    from datetime import datetime as _dt
    if not _re.fullmatch(r'\d{12}', lidb or ''):
        return None
    try:
        return _dt.strptime(lidb, '%Y%m%d%H%M')
    except Exception:
        return None


_BV_ALL = {'at': 0, 'records': None}


def bv_all_records(fresh=False):
    """Every number in the BulkVS account. Kept for a minute — the health
    check and a send close together should not each ask for the whole list."""
    import time as _t
    if not fresh and _BV_ALL['records'] is not None and _t.time() - _BV_ALL['at'] < 600:
        return _BV_ALL['records']
    ans = _bv('GET', '/tnRecord', timeout=40)
    recs = [r for r in (ans if isinstance(ans, list) else [ans]) if isinstance(r, dict)]
    _BV_ALL.update(at=_t.time(), records=recs)
    return recs


def _patch_cached(ten, tcr=None, lidb=None, webhook=None, remove=False):
    """After our own change, correct the remembered list rather than throw it
    away — downloading every number again on each send is what made it slow."""
    recs = _BV_ALL.get('records')
    if recs is None:
        return
    for r in list(recs):
        if _rec_tn(r) == ten:
            if remove:
                recs.remove(r)
                return
            m = r.setdefault('Messaging', {})
            if tcr is not None:
                m['TCR'] = tcr
            if webhook is not None:
                m['Webhook'] = webhook
            if lidb is not None:
                r['Lidb'] = lidb
            return


def bv_record(number, fresh=False):
    """BulkVS's record for this number, or None. Their filter is not always
    honoured, so the answer is checked to be about this number before it is
    believed, and the whole list is searched if it is not."""
    ten = _ten(number)
    if not fresh and _BV_ALL.get('records') is not None:
        import time as _t
        if _t.time() - _BV_ALL['at'] < 600:
            for r in _BV_ALL['records']:
                if _rec_tn(r) == ten:
                    return r
    try:
        ans = _bv('GET', '/tnRecord?Number=1' + ten)
        for r in (ans if isinstance(ans, list) else [ans]):
            if isinstance(r, dict) and _rec_tn(r) == ten:
                return r
    except BulkVSError:
        pass
    for r in bv_all_records():
        if _rec_tn(r) == ten:
            return r
    return None


def bv_campaigns():
    ans = _bv('GET', '/campaigns')
    out = []
    for c in (ans if isinstance(ans, list) else [ans]):
        if not isinstance(c, dict):
            continue
        tcr = (c.get('TCR') or c.get('Tcr') or c.get('tcr') or '').strip()
        cnt = c.get('Count', c.get('count', c.get('TN Count')))
        try:
            cnt = int(cnt)
        except Exception:
            cnt = None
        if tcr:
            out.append({'tcr': tcr, 'count': cnt, 'raw': c})
    return out


def static_numbers():
    """Numbers fixed to a campaign by hand: AccountSMS with StaticCompign = 1."""
    conn = cms_db._connect()
    try:
        cu = conn.cursor()
        cu.execute("""SELECT SMSNumber, SMSCompaign, AccountID FROM AccountSMS
                      WHERE ISNULL(StaticCompign, 0) = 1""")
        out = {}
        for r in cu.fetchall():
            if r[0]:
                out[_ten(r[0])] = {'tcr': (r[1] or '').strip(),
                                   'account_id': int(r[2]) if r[2] else None}
        return out
    finally:
        conn.close()


def campaign_plan(number, statics=None):
    """What has to happen before this number can send. Reads only."""
    ten = _ten(number)
    statics = static_numbers() if statics is None else statics
    if ten in statics and statics[ten]['tcr']:
        return {'step': 'static', 'tcr': statics[ten]['tcr'],
                'meaning': 'Static number, always on %s — nothing to change.' % statics[ten]['tcr']}
    rec = bv_record(ten)
    if rec is None:
        return {'step': 'missing',
                'meaning': 'BulkVS does not have %s, so it cannot send.' % ten}
    if _rec_tcr(rec):
        return {'step': 'already', 'tcr': _rec_tcr(rec),
                'meaning': 'Already on campaign %s — it can send.' % _rec_tcr(rec)}
    camps = bv_campaigns()
    if not camps or any(c['count'] is None for c in camps):
        # never take a number off on a guess
        return {'step': 'stuck', 'campaigns': camps,
                'meaning': ('Could not read how full the campaigns are from BulkVS, so nothing '
                            'was changed. Raw answer: %s' % str([c.get('raw') for c in camps])[:200])}
    for c in camps:
        if c['count'] < CAMPAIGN_LIMIT:
            return {'step': 'join', 'tcr': c['tcr'], 'campaigns': camps,
                    'meaning': 'Will join campaign %s (%d of %d used).'
                               % (c['tcr'], c['count'], CAMPAIGN_LIMIT)}
    # both full: take off the one WE put on longest ago — never a static one,
    # never one somebody set up by hand
    movable = []
    for r in bv_all_records():
        t = _lidb_time(_rec_lidb(r))
        if (_rec_tcr(r) and t is not None and _rec_tn(r) not in statics
                and _rec_tn(r) != ten and _rec_webhook(r) in ('WebHook2', '')):
            movable.append((t, r))
    if not movable:
        return {'step': 'stuck', 'campaigns': camps,
                'meaning': ('Both campaigns are full and no number can safely be taken off '
                            '(the rest are static or were set up by hand).')}
    movable.sort(key=lambda x: x[0])
    t, r = movable[0]
    tcr = _rec_tcr(r)
    # a campaign that is over the limit is brought back down to it: take off
    # enough of its oldest so that, with this number added, it sits at the limit
    count = next((c['count'] for c in camps if c['tcr'] == tcr), CAMPAIGN_LIMIT)
    extra = [_rec_tn(x) for _, x in movable[1:] if _rec_tcr(x) == tcr][:max(0, count - CAMPAIGN_LIMIT)]
    return {'step': 'swap', 'tcr': tcr, 'move_out': _rec_tn(r), 'extra_out': extra,
            'move_out_since': t.isoformat(), 'campaigns': camps,
            'meaning': ('Both campaigns are full. Will take %s off %s (on since %s)%s '
                        'and put this number on instead.'
                        % (_rec_tn(r), tcr, t.strftime('%b %d %Y %I:%M %p'),
                           (' and %d more to bring it down to %d' % (len(extra), CAMPAIGN_LIMIT))
                           if extra else ''))}


import threading as _threading
import contextlib as _contextlib
_CAMPAIGN_THREAD_LOCK = _threading.Lock()


@_contextlib.contextmanager
def _campaign_lock():
    """Only one campaign change at a time, across every worker — so two sends
    at the same moment can never both take the last place and push a
    campaign past the limit."""
    with _CAMPAIGN_THREAD_LOCK:
        conn = None
        try:
            from server import get_db
            conn = get_db()
            cu = conn.cursor()
            cu.execute("SET lock_timeout = '30s'")
            cu.execute('SELECT pg_advisory_lock(80422)')
        except Exception as e:
            print('[sms] campaign lock only within this worker: ' + str(e)[:100])
            conn = None
        try:
            yield
        finally:
            if conn is not None:
                try:
                    conn.cursor().execute('SELECT pg_advisory_unlock(80422)')
                    conn.close()
                except Exception:
                    pass


def _put_on_campaign(ten, tcr, lidb=None, webhook=None, removed_already=0):
    """The old CMS's updateTCR. An empty campaign takes the number off.

    The last line of defence for the limit: before putting a number ON, the
    campaign's count is read again from BulkVS, and the change is refused if
    it would go past CAMPAIGN_LIMIT. 'removed_already' counts numbers taken
    off this campaign a moment ago that BulkVS may not have counted yet."""
    import time as _t
    from datetime import datetime as _dt
    if tcr:
        camps = {c['tcr']: c['count'] for c in bv_campaigns()}
        if tcr not in camps:
            raise WriteRefused('There is no campaign called %s at BulkVS.' % tcr)
        if camps[tcr] is None:
            raise WriteRefused('Could not read how full %s is, so nothing was changed.' % tcr)
        if camps[tcr] - removed_already >= CAMPAIGN_LIMIT:
            raise WriteRefused('%s already has %d numbers — the limit is %d. Take one off first.'
                               % (tcr, camps[tcr] - removed_already, CAMPAIGN_LIMIT))
        body = {'TN': '1' + ten, 'Lidb': lidb or _dt.now().strftime('%Y%m%d%H%M'),
                'Webhook': webhook or 'WebHook2', 'Tcr': tcr}
    else:
        body = {'TN': '1' + ten, 'Lidb': 'PROCLICK', 'Webhook': webhook or 'WebHook1', 'Tcr': ''}
    _bv('POST', '/tnRecord', body)
    _patch_cached(ten, tcr=body['Tcr'], lidb=body['Lidb'], webhook=body['Webhook'])
    _ACTIVITY['data'] = None
    _t.sleep(0.5)


def apply_campaign_plan(number, plan):
    """Carries out the campaign step. The plan is worked out again inside the
    lock, from fresh BulkVS figures, so it cannot act on a stale picture."""
    ten = _ten(number)
    with _campaign_lock():
        plan = campaign_plan(ten)
        if plan['step'] == 'join':
            _put_on_campaign(ten, plan['tcr'])
        elif plan['step'] == 'swap':
            gone = 0
            for out_ in [plan['move_out']] + list(plan.get('extra_out') or []):
                _put_on_campaign(out_, '')
                gone += 1
            _put_on_campaign(ten, plan['tcr'], removed_already=gone)
    return plan


def _old_cms_escape(message):
    """The old CMS's escaping. It writes these characters as \\uXXXX codes
    INSIDE a JSON body it builds by hand, so BulkVS reads them back as the
    ordinary characters. A backslash, tab and other control characters are
    covered too, which the old code left out (a message with one broke)."""
    m = (message or '').replace('\\', '\\u005C')
    for ch, code in (('@', '\\u0040'), ('?', '\\u003F'), ('$', '\\u0024'),
                     ('#', '\\u0023'), ("'", '\\u0027'), ('*', '\\u002A'),
                     ('_', '\\u005F'), ('+', '\\u002B'), ('&', '\\u0026'),
                     (';', '\\u003B'), ('.', '\\u002E'), ('=', '\\u003D'),
                     ('"', '\\u0022')):
        m = m.replace(ch, code)
    m = m.replace('\r', '\\u000D').replace('\n', '\\n').replace('\t', '\\t')
    return ''.join(ch if ord(ch) >= 32 else '\\u%04X' % ord(ch) for ch in m)


def _old_cms_send_body(frm, to, message, media_url=None):
    """The messageSend body exactly as the old CMS puts it on the wire."""
    body = (' {\n"From": "' + frm + '",\n"To": ["' + to + '"],\n"Message": "'
            + _old_cms_escape(message) + '"\n ')
    if media_url:
        body += ',"MediaURLs":[ "' + media_url.replace('\\', '\\\\').replace('"', '%22') + '"]\n'
    return body + ' }'


def _send_via_bulkvs(body, tries=6, wait=10):
    """messageSend, waiting while BulkVS says the number is not ready yet
    (it takes a little while after joining a campaign). Gives up after about
    a minute rather than hanging for ever as the old loop could."""
    import time as _t
    last = None
    for i in range(tries):
        try:
            if isinstance(body, str):
                return _bv('POST', '/messageSend', raw=body, timeout=45)
            return _bv('POST', '/messageSend', body, timeout=45)
        except BulkVSError as e:
            last = e
            if e.code != NOT_READY_CODE:
                raise
            if i < tries - 1:
                _t.sleep(wait)
    raise BulkVSError('The number is still not ready to send after %d tries: %s'
                      % (tries, last), code=NOT_READY_CODE)


def _area_code(account_row_phone):
    ten = _ten(account_row_phone)
    return ten[:3] if len(ten) == 10 else ''


def _number_in_cms(ten):
    """Is this number already ours — in the pool or on an account?"""
    conn = cms_db._connect()
    try:
        cu = conn.cursor()
        cu.execute("SELECT COUNT(*) FROM Account WHERE RIGHT(ISNULL(smsNumber,''), 10) = %s", (ten,))
        on_acct = int(cu.fetchone()[0] or 0)
        cu.execute("SELECT COUNT(*) FROM SMSNumberTable WHERE RIGHT([%s], 10) = %%s" % cms_db.pool_col(), (ten,))
        in_pool = int(cu.fetchone()[0] or 0)
        return on_acct, in_pool
    finally:
        conn.close()


def order_number(npa, who):
    """Buy one number in this area code from BulkVS and put it in the spare
    pool — the old CMS's orderTN. Costs money, so callers confirm first."""
    npa = ''.join(ch for ch in str(npa or '') if ch.isdigit())[:3]
    if len(npa) != 3:
        raise WriteRefused('Give a three-digit area code.')
    out = {'table': 'SMSNumberTable', 'action': 'order_number', 'dry_run': False,
           'values': {'area_code': npa}}
    try:
        avail = _bv('GET', '/orderTn?Npa=%s&Lca=false&Limit=1' % npa)
        avail = [a for a in (avail if isinstance(avail, list) else [avail]) if isinstance(a, dict)]
        if not avail or not _rec_tn(avail[0]):
            raise WriteRefused('BulkVS has no numbers to sell in area code %s right now.' % npa)
        tn = _rec_tn(avail[0])
        on_acct, in_pool = _number_in_cms(tn)
        if on_acct or in_pool:
            raise WriteRefused('BulkVS offered %s, but we already have it. Try again.' % tn)
        ans = _bv('POST', '/orderTn', {
            'TN': '1' + tn, 'Lidb': 'PROCLICK',
            'Portout Pin': os.getenv('BULKVS_PORTOUT_PIN', '1111111'),
            'ReferenceID': '', 'Trunk Group': SMS_TRUNK,
            'Sms': True, 'Mms': True, 'Webhook': 'WebHook1'}, timeout=45)
        got = _rec_tn(ans) if isinstance(ans, dict) else ''
        if not got:
            raise BulkVSError('BulkVS did not confirm the order: %s' % str(ans)[:160])
        conn = cms_db._connect()
        try:
            cu = conn.cursor()
            cu.execute('SET TRANSACTION ISOLATION LEVEL READ COMMITTED')
            cu.execute('INSERT INTO SMSNumberTable ([%s], Created) VALUES (%%s, GETDATE())' % cms_db.pool_col(), (got,))
            conn.commit()
        finally:
            conn.close()
        _BV_ALL['records'] = None
        out.update(ok=True, committed=True, number=got,
                   meaning='Bought %s and added it to the spare pool.' % got)
        out['values']['number'] = got
    except WriteRefused:
        raise
    except Exception as e:
        out.update(ok=False, committed=False, error=str(e)[:300])
    _log(who, out)
    return out


def _take_spare(npa):
    """The oldest spare in this area code, removed from the pool and returned.
    Locked, so two agents can never be handed the same number."""
    conn = cms_db._connect()
    try:
        cu = conn.cursor()
        cu.execute('SET TRANSACTION ISOLATION LEVEL READ COMMITTED')
        cu.execute("EXEC sp_getapplock @Resource = 'SMSNumberTable', @LockMode = 'Exclusive', "
                   "@LockOwner = 'Transaction', @LockTimeout = 15000")
        for _ in range(5):
            pc = cms_db.pool_col()
            cu.execute("""SELECT TOP 1 [%s] FROM SMSNumberTable
                          WHERE [%s] LIKE %%s ORDER BY Created, Id""" % (pc, pc), (npa + '%',))
            r = cu.fetchone()
            if not r:
                conn.commit()
                return None
            num = (r[0] or '').strip()
            cu.execute('DELETE FROM SMSNumberTable WHERE [%s] = %%s' % cms_db.pool_col(), (num,))
            cu.execute("SELECT COUNT(*) FROM Account WHERE RIGHT(ISNULL(smsNumber,''), 10) = %s",
                       (_ten(num),))
            if int(cu.fetchone()[0] or 0) == 0:
                conn.commit()
                return num
            # already a customer's — it should not have been in the pool; drop it, try the next
        conn.commit()
        return None
    except Exception:
        try:
            conn.rollback()
        except Exception:
            pass
        raise
    finally:
        conn.close()


def activate_texting(account_id, who, npa=None):
    """Give a customer a texting number — the old CMS's ActivateSMS. Takes
    the oldest spare in their area code; if there is none, buys one first
    (the old CMS bought one and made the agent press the button again). Then
    buys a replacement so the pool stays topped up."""
    account_id = int(account_id)
    conn = cms_db._connect()
    try:
        cu = conn.cursor()
        cu.execute("""SELECT smsNumber, ISNULL(smsActivate,0), Phone, Mobile, FirstName, LastName
                      FROM Account WHERE Id = %s""", (account_id,))
        row = cu.fetchone()
    finally:
        conn.close()
    if not row:
        raise WriteRefused('There is no account with id %d.' % account_id)
    current, active, phone, mobile, first, last = row
    name = ('%s %s' % (first or '', last or '')).strip() or 'This account'
    if (current or '').strip() and int(active or 0) == 1:
        raise WriteRefused('%s already has texting on %s.' % (name, current.strip()))
    if (current or '').strip():
        # a number is still on the account, just switched off — switch it back
        # on rather than spend a new one, if BulkVS still has it
        if bv_record(current) is not None:
            conn = cms_db._connect()
            try:
                cu = conn.cursor()
                cu.execute('SET TRANSACTION ISOLATION LEVEL READ COMMITTED')
                cu.execute("UPDATE Account SET smsActivate = 1, SMSDateEnd = '9999-12-31' WHERE Id = %s",
                           (account_id,))
                conn.commit()
            finally:
                conn.close()
            out = {'table': 'Account', 'action': 'activate_texting', 'row_id': account_id,
                   'dry_run': False, 'values': {'number': current.strip(), 'reused': True},
                   'ok': True, 'committed': True, 'number': current.strip(),
                   'meaning': '%s can text again from %s.' % (name, current.strip())}
            _log(who, out)
            return out
    npa = ''.join(ch for ch in str(npa or '') if ch.isdigit())[:3] or _area_code(phone or mobile)
    if len(npa) != 3:
        raise WriteRefused('%s has no phone number to take an area code from. '
                           'Give one.' % name)

    out = {'table': 'Account', 'action': 'activate_texting', 'row_id': account_id,
           'dry_run': False, 'values': {'area_code': npa}}
    bought_first = None
    num = _take_spare(npa)
    if not num:
        o = order_number(npa, who)
        if not o.get('ok'):
            raise WriteRefused('No spare %s number, and buying one failed: %s'
                               % (npa, o.get('error') or '?'))
        bought_first = o['number']
        num = _take_spare(npa)
        if not num:
            raise WriteRefused('Bought %s but could not take it from the pool. '
                               'Look at SMS Numbers.' % bought_first)
    conn = cms_db._connect()
    try:
        cu = conn.cursor()
        cu.execute('SET TRANSACTION ISOLATION LEVEL READ COMMITTED')
        cu.execute("""UPDATE Account SET smsActivate = 1, smsNumber = %s,
                             SMSDateEnd = '9999-12-31' WHERE Id = %s""", (num, account_id))
        conn.commit()
    except Exception as e:
        try:
            conn.rollback()
        except Exception:
            pass
        # put the number back so it is not lost
        try:
            c2 = cms_db._connect(); k = c2.cursor()
            k.execute('INSERT INTO SMSNumberTable ([%s], Created) VALUES (%%s, GETDATE())' % cms_db.pool_col(), (num,))
            c2.commit(); c2.close()
        except Exception:
            pass
        out.update(ok=False, committed=False, error=_explain(e))
        _log(who, out)
        return out
    finally:
        conn.close()

    out.update(ok=True, committed=True, number=num, bought_first=bought_first)
    out['values']['number'] = num
    # top the pool back up, as the old CMS does every time
    try:
        refill = order_number(npa, who)
        out['refill'] = refill.get('number') if refill.get('ok') else None
        if not refill.get('ok'):
            out['refill_warning'] = 'Could not buy a replacement spare: ' + str(refill.get('error'))[:160]
    except Exception as e:
        out['refill'] = None
        out['refill_warning'] = 'Could not buy a replacement spare: ' + str(e)[:160]
    out['meaning'] = '%s can now text from %s.' % (name, num)
    _log(who, out)
    return out


def disable_texting(account_id, who):
    """Turn texting off — the old CMS's DisableSMS. Releases the number at
    BulkVS (it is gone for good) and clears it from the account. If BulkVS
    will not release it, the account is left exactly as it was."""
    account_id = int(account_id)
    conn = cms_db._connect()
    try:
        cu = conn.cursor()
        cu.execute('SELECT smsNumber, FirstName, LastName FROM Account WHERE Id = %s', (account_id,))
        row = cu.fetchone()
    finally:
        conn.close()
    if not row:
        raise WriteRefused('There is no account with id %d.' % account_id)
    num = (row[0] or '').strip()
    name = ('%s %s' % (row[1] or '', row[2] or '')).strip() or 'This account'
    if not num:
        raise WriteRefused('%s has no texting number.' % name)
    ten = _ten(num)
    if ten in static_numbers():
        raise WriteRefused('%s is a static campaign number. Releasing it would take it off '
                           'its campaign for good — that has to be done by hand.' % ten)
    out = {'table': 'Account', 'action': 'disable_texting', 'row_id': account_id,
           'dry_run': False, 'values': {'number': num}}
    if bv_record(ten) is None:
        released = 'BulkVS no longer had it'
    else:
        _bv('DELETE', '/tnRecord?Number=' + num)
        _BV_ALL['records'] = None
        released = 'released at BulkVS'
    conn = cms_db._connect()
    try:
        cu = conn.cursor()
        cu.execute('SET TRANSACTION ISOLATION LEVEL READ COMMITTED')
        cu.execute("""UPDATE Account SET smsActivate = 0, smsNumber = '',
                             SMSDateEnd = '9999-12-31' WHERE Id = %s""", (account_id,))
        conn.commit()
    finally:
        conn.close()
    out.update(ok=True, committed=True, released=released,
               meaning='Texting is off for %s. %s was %s.' % (name, num, released))
    _log(who, out)
    return out


def sms_health():
    """Everything about the texting setup on one page, read only: the two
    campaigns, the static numbers, which number would be taken off next, the
    spare pool, and anything that disagrees between the CMS and BulkVS."""
    from collections import Counter
    out = {'configured': bv_configured(), 'campaign_limit': CAMPAIGN_LIMIT, 'problems': []}
    statics = static_numbers()
    conn = cms_db._connect()
    try:
        cu = conn.cursor()
        cu.execute('SELECT [%s], Created FROM SMSNumberTable ORDER BY Created' % cms_db.pool_col())
        pool = [{'number': (r[0] or '').strip(), 'added': cms_db._plain(r[1])} for r in cu.fetchall()]
        cu.execute("""SELECT Id, FirstName, LastName, smsNumber, ISNULL(smsActivate,0)
                      FROM Account WHERE ISNULL(smsNumber,'') <> ''""")
        accounts = [{'account_id': int(r[0]), 'customer': ('%s %s' % (r[1] or '', r[2] or '')).strip(),
                     'number': (r[3] or '').strip(), 'active': bool(r[4])} for r in cu.fetchall()]
        cu.execute("""SELECT RIGHT(AccountSMSNumber,10), MAX(smsDate), COUNT(*) FROM SMSLog
                      WHERE InOut = 'Out' AND smsDate >= DATEADD(day, -30, GETDATE())
                      GROUP BY RIGHT(AccountSMSNumber,10)""")
        sent = {r[0]: {'last_sent': cms_db._plain(r[1]), 'sent_30d': int(r[2] or 0)} for r in cu.fetchall()}
    finally:
        conn.close()
    by_area = Counter(_ten(p['number'])[:3] for p in pool)
    out['pool'] = {'count': len(pool), 'by_area_code': dict(sorted(by_area.items())), 'numbers': pool}
    out['accounts_with_numbers'] = len(accounts)
    acct_by = {}
    for a in accounts:
        acct_by.setdefault(_ten(a['number']), []).append(a)
    for ten, lst in acct_by.items():
        if len(lst) > 1:
            out['problems'].append({'kind': 'shared', 'number': ten,
                                    'text': '%s is on %d accounts: %s' % (ten, len(lst),
                                            ', '.join(x['customer'] or str(x['account_id']) for x in lst))})
    for p in pool:
        if _ten(p['number']) in acct_by:
            out['problems'].append({'kind': 'both', 'number': _ten(p['number']),
                                    'text': '%s is in the spare pool AND on a customer' % _ten(p['number'])})
    if not out['configured']:
        out['problems'].append({'kind': 'config', 'text': 'BULKVS_AUTH is not set, so BulkVS cannot be checked.'})
        out['static'] = [{'number': k, **v} for k, v in statics.items()]
        return out

    recs = bv_all_records(fresh=True)
    by_tn = {_rec_tn(r): r for r in recs}
    camps = bv_campaigns()
    on_camp = Counter(_rec_tcr(r) for r in recs if _rec_tcr(r))
    out['campaigns'] = [{'tcr': c['tcr'], 'count': c['count'],
                         'counted_here': on_camp.get(c['tcr'], 0),
                         'static': len([1 for k, v in statics.items() if v['tcr'] == c['tcr']]),
                         'free': (CAMPAIGN_LIMIT - c['count']) if c['count'] is not None else None}
                        for c in camps]
    out['static'] = []
    for ten, v in sorted(statics.items()):
        r = by_tn.get(ten)
        st = {'number': ten, 'tcr': v['tcr'], 'account_id': v['account_id'],
              'at_bulkvs': r is not None, 'bulkvs_tcr': _rec_tcr(r) if r else None,
              'webhook': _rec_webhook(r) if r else None,
              'on_its_campaign': bool(r and _rec_tcr(r) == v['tcr'])}
        out['static'].append(st)
        if r is None:
            out['problems'].append({'kind': 'static_missing', 'number': ten,
                                    'text': 'Static number %s is not at BulkVS' % ten})
        elif not st['on_its_campaign']:
            out['problems'].append({'kind': 'static_off', 'number': ten,
                                    'text': 'Static number %s should be on %s but BulkVS has it on %s'
                                            % (ten, v['tcr'], _rec_tcr(r) or 'no campaign')})
    rot = []
    for r in recs:
        t = _lidb_time(_rec_lidb(r))
        if _rec_tcr(r) and _rec_tn(r) not in statics:
            rot.append({'number': _rec_tn(r), 'tcr': _rec_tcr(r),
                        'since': t.isoformat() if t else None,
                        'movable': t is not None,
                        'lidb': _rec_lidb(r), 'webhook': _rec_webhook(r),
                        'customer': (acct_by.get(_rec_tn(r)) or [{}])[0].get('customer'),
                        **sent.get(_rec_tn(r), {})})
    rot.sort(key=lambda x: (not x['movable'], x['since'] or ''))
    out['rotating'] = rot
    for x in rot:
        if not x['movable']:
            out['problems'].append({'kind': 'hand_set', 'number': x['number'],
                                    'text': ('%s is on %s but was not put there by the CMS (Lidb "%s"), '
                                             'so it will never be taken off — it holds a place permanently'
                                             % (x['number'], x['tcr'], x['lidb']))})
    # numbers the CMS hands out that BulkVS does not have — they cannot send
    for ten, lst in acct_by.items():
        if any(a['active'] for a in lst) and ten not in by_tn:
            out['problems'].append({'kind': 'account_missing', 'number': ten,
                                    'text': '%s is %s\'s texting number but BulkVS does not have it'
                                            % (ten, lst[0]['customer'] or lst[0]['account_id'])})
    for p in pool:
        if _ten(p['number']) not in by_tn:
            out['problems'].append({'kind': 'pool_missing', 'number': _ten(p['number']),
                                    'text': 'Spare %s is not at BulkVS — handing it out would fail'
                                            % _ten(p['number'])})
    ours = set(acct_by) | {_ten(p['number']) for p in pool} | set(statics)
    orphans = []
    for r in recs:
        routing = r.get('Routing') or {}
        trunk = routing.get('Trunk Group') or r.get('Trunk Group') or ''
        if trunk == SMS_TRUNK and _rec_tn(r) not in ours:
            orphans.append(_rec_tn(r))
    out['not_in_cms'] = orphans
    if orphans:
        out['problems'].append({'kind': 'orphans',
                                'text': '%d texting numbers at BulkVS belong to nobody in the CMS (you may be paying for them)'
                                        % len(orphans)})
    for c in out['campaigns']:
        if c['count'] is not None and c['count'] != c['counted_here']:
            out['problems'].append({'kind': 'count_mismatch',
                                    'text': ('BulkVS says %s has %d numbers but %d of our numbers show it — '
                                             'some may be outside this account or still changing'
                                             % (c['tcr'], c['count'], c['counted_here']))})
    if not out['campaigns']:
        out['problems'].append({'kind': 'no_campaigns',
                                'text': 'BulkVS returned no campaigns — sending would not be able to join one'})
    out['bulkvs_numbers'] = len(recs)
    return out


_ACTIVITY = {'at': 0, 'data': None}


def sms_activity(fresh=False):
    """Every texting number we have anywhere — on a customer, in the spare
    pool, or only at BulkVS — with when it last sent and last received a
    message, so numbers nobody uses stand out. Reads only. Kept 10 minutes."""
    import time as _t
    if not fresh and _ACTIVITY['data'] is not None and _t.time() - _ACTIVITY['at'] < 600:
        return _ACTIVITY['data']
    statics = static_numbers()
    conn = cms_db._connect()
    try:
        cu = conn.cursor()
        cu.execute("""SELECT RIGHT(AccountSMSNumber, 10),
                             MAX(CASE WHEN InOut = 'Out' THEN smsDate END),
                             MAX(CASE WHEN InOut = 'In' THEN smsDate END),
                             SUM(CASE WHEN smsDate >= DATEADD(day, -90, GETDATE()) THEN 1 ELSE 0 END),
                             COUNT(*)
                      FROM SMSLog WHERE ISNULL(AccountSMSNumber, '') <> ''
                      GROUP BY RIGHT(AccountSMSNumber, 10)""")
        log = {}
        for r in cu.fetchall():
            log[_ten(r[0])] = (r[1], r[2], int(r[3] or 0), int(r[4] or 0))
        cu.execute("""SELECT Id, FirstName, LastName, smsNumber, ISNULL(smsActivate, 0)
                      FROM Account WHERE ISNULL(smsNumber, '') <> ''""")
        accounts = cu.fetchall()
        cu.execute('SELECT [%s], Created FROM SMSNumberTable' % cms_db.pool_col())
        pool = cu.fetchall()
    finally:
        conn.close()

    rows = {}

    def row(ten):
        return rows.setdefault(ten, {'number': ten, 'customer': None, 'account_id': None,
                                     'customers_on_it': 0, 'active': None, 'where': 'nobody',
                                     'static_tcr': None, 'campaign': None, 'at_bulkvs': None,
                                     'last_sent': None, 'last_received': None,
                                     'last_activity': None, 'messages_90d': 0, 'messages_total': 0})

    for aid, fn, ln, num, act in accounts:
        ten = _ten(num)
        if len(ten) < 10:
            continue
        x = row(ten)
        x['customers_on_it'] += 1
        if x['account_id'] is None or act:
            x.update(customer=('%s %s' % (fn or '', ln or '')).strip() or None,
                     account_id=int(aid), active=bool(act), where='customer')
    for num, created in pool:
        ten = _ten(num)
        if len(ten) == 10:
            x = row(ten)
            x['where'] = 'customer and spare pool' if x['where'] == 'customer' else 'spare pool'
            x['added_to_pool'] = cms_db._plain(created)
    for ten, v in statics.items():
        row(ten)['static_tcr'] = v['tcr']

    bv_ok = False
    if bv_configured():
        try:
            for r in bv_all_records(fresh=True):
                ten = _rec_tn(r)
                routing = r.get('Routing') or {}
                trunk = routing.get('Trunk Group') or r.get('Trunk Group') or ''
                if ten in rows or trunk == SMS_TRUNK:
                    x = row(ten)
                    x['at_bulkvs'] = True
                    x['campaign'] = _rec_tcr(r) or None
            for x in rows.values():
                if x['at_bulkvs'] is None:
                    x['at_bulkvs'] = False
            bv_ok = True
        except Exception as e:
            print('[sms] activity could not read BulkVS: ' + str(e)[:120])

    for ten, x in rows.items():
        if ten in log:
            out_, in_, n90, ntot = log[ten]
            x['last_sent'] = cms_db._plain(out_)
            x['last_received'] = cms_db._plain(in_)
            x['messages_90d'], x['messages_total'] = n90, ntot
            dates = [d for d in (x['last_sent'], x['last_received']) if d]
            x['last_activity'] = max(dates) if dates else None

    data = {'numbers': sorted(rows.values(), key=lambda x: (x['last_activity'] or '', x['number'])),
            'bulkvs_checked': bv_ok, 'count': len(rows)}
    _ACTIVITY.update(at=_t.time(), data=data)
    return data


# ─── Managing one number by hand, with guardrails ───────────────────────────
def number_info(number):
    """Everything about one number: BulkVS's record, the CMS's static list,
    who has it, and what a send would do. Reads only."""
    ten = _ten(number)
    if len(ten) != 10:
        raise WriteRefused('Give a ten-digit number.')
    statics = static_numbers()
    rec = bv_record(ten)
    camps = bv_campaigns()
    conn = cms_db._connect()
    try:
        cu = conn.cursor()
        cu.execute("""SELECT TOP 5 Id, FirstName, LastName, ISNULL(smsActivate,0) FROM Account
                      WHERE RIGHT(ISNULL(smsNumber,''), 10) = %s""", (ten,))
        customers = [{'account_id': int(r[0]), 'customer': ('%s %s' % (r[1] or '', r[2] or '')).strip(),
                      'active': bool(r[3])} for r in cu.fetchall()]
        cu.execute("SELECT COUNT(*) FROM SMSNumberTable WHERE RIGHT([%s], 10) = %%s" % cms_db.pool_col(), (ten,))
        in_pool = int(cu.fetchone()[0] or 0) > 0
        cu.execute("""SELECT MAX(CASE WHEN InOut = 'Out' THEN smsDate END),
                             MAX(CASE WHEN InOut = 'In' THEN smsDate END)
                      FROM SMSLog WHERE RIGHT(AccountSMSNumber, 10) = %s""", (ten,))
        r = cu.fetchone() or (None, None)
    finally:
        conn.close()
    m = (rec or {}).get('Messaging') or {}
    out = {'number': ten, 'at_bulkvs': rec is not None,
           'campaign': _rec_tcr(rec) if rec else None,
           'webhook': _rec_webhook(rec) if rec else None,
           'lidb': _rec_lidb(rec) if rec else None,
           'put_on_by_cms_at': (_lidb_time(_rec_lidb(rec)).isoformat()
                                if rec and _lidb_time(_rec_lidb(rec)) else None),
           'sms_enabled': m.get('Sms'), 'mms_enabled': m.get('Mms'),
           'static_tcr': (statics.get(ten) or {}).get('tcr'),
           'customers': customers, 'in_pool': in_pool,
           'last_sent': cms_db._plain(r[0]), 'last_received': cms_db._plain(r[1]),
           'campaigns': [{'tcr': c['tcr'], 'count': c['count'], 'limit': CAMPAIGN_LIMIT} for c in camps],
           'limit': CAMPAIGN_LIMIT, 'record': rec}
    # plain-language warnings, so a problem is visible before anything is touched
    w = []
    if rec is None:
        w.append('BulkVS does not have this number.')
    else:
        if m.get('Sms') is False:
            w.append('SMS is switched OFF for this number at BulkVS — it cannot text at all.')
        if out['static_tcr'] and out['campaign'] != out['static_tcr']:
            w.append('The CMS has it as static on %s, but BulkVS has it on %s.'
                     % (out['static_tcr'], out['campaign'] or 'no campaign'))
        if out['campaign'] and not out['static_tcr'] and not out['put_on_by_cms_at']:
            w.append('It is on %s but is not on the CMS\'s static list, and was not put there by '
                     'the CMS (Lidb "%s"). If it is meant to be static, press "Make static" so the '
                     'CMS knows to leave it alone.' % (out['campaign'], out['lidb'] or 'blank'))
        if out['webhook'] not in ('WebHook1', 'WebHook2'):
            w.append('Its webhook is "%s", not WebHook1 or WebHook2 — replies to it may not reach '
                     'the CMS.' % (out['webhook'] or 'blank'))
    for c in camps:
        if c['count'] is not None and c['count'] > CAMPAIGN_LIMIT:
            w.append('%s has %d numbers, over the limit of %d. The next automatic swap on it '
                     'brings it back down.' % (c['tcr'], c['count'], CAMPAIGN_LIMIT))
    out['warnings'] = w
    return out


def _set_static_row(ten, tcr, static):
    """Adds or updates this number on the CMS's static list (AccountSMS)."""
    conn = cms_db._connect()
    try:
        cu = conn.cursor()
        cu.execute('SET TRANSACTION ISOLATION LEVEL READ COMMITTED')
        cu.execute('SELECT id FROM AccountSMS WHERE RIGHT(SMSNumber, 10) = %s', (ten,))
        have = cu.fetchone()
        if have:
            if tcr:
                cu.execute('UPDATE AccountSMS SET StaticCompign = %s, SMSCompaign = %s WHERE id = %s',
                           (1 if static else 0, tcr, int(have[0])))
            else:
                cu.execute('UPDATE AccountSMS SET StaticCompign = %s WHERE id = %s',
                           (1 if static else 0, int(have[0])))
        elif static:
            cu.execute("""SELECT TOP 1 Id FROM Account
                          WHERE RIGHT(ISNULL(smsNumber,''), 10) = %s ORDER BY Id DESC""", (ten,))
            a = cu.fetchone()
            cu.execute("""INSERT INTO AccountSMS (AccountID, Created, SMSNumber, SMSCompaign, StaticCompign)
                          VALUES (%s, GETDATE(), %s, %s, 1)""", (int(a[0]) if a else None, ten, tcr))
        conn.commit()
    finally:
        conn.close()


def number_campaign_action(number, action, who, tcr=None, reason=None):
    """Take a number off its campaign, put it on one, or make it static or not.

    Guardrails: never past CAMPAIGN_LIMIT on any campaign (checked against
    BulkVS at the moment of change); a static number cannot be taken off or
    moved until it is made not-static first; a number BulkVS does not have, or
    with SMS switched off, cannot be put on; one change at a time; every
    change needs a reason and is logged; and BulkVS is read back afterwards
    to prove it took.
    """
    ten = _ten(number)
    reason = (reason or '').strip()
    if action not in ('off', 'on', 'make_static', 'unstatic'):
        raise WriteRefused('Unknown action.')
    if len(reason) < 3:
        raise WriteRefused('Write why — it is kept in the log.')
    with _campaign_lock():
        _BV_ALL['records'] = None
        info = number_info(ten)
        if not info['at_bulkvs']:
            raise WriteRefused('BulkVS does not have %s.' % ten)
        current, static = info['campaign'], info['static_tcr']
        names = {c['tcr']: c['count'] for c in info['campaigns']}
        if action in ('on', 'make_static'):
            tcr = (tcr or current or '').strip()
            if tcr not in names:
                raise WriteRefused('Choose one of the campaigns: %s.' % ', '.join(names))
            if info['sms_enabled'] is False:
                raise WriteRefused('SMS is switched off for %s at BulkVS — turn it on there first.' % ten)
        if action == 'off':
            if static:
                raise WriteRefused('%s is static on %s. Press "Remove static" first if you really '
                                   'mean to take it off.' % (ten, static))
            if not current:
                raise WriteRefused('%s is not on a campaign.' % ten)
            _put_on_campaign(ten, '', webhook=info['webhook'])
        elif action == 'on':
            if static and static != tcr:
                raise WriteRefused('%s is static on %s. Remove static first to move it.' % (ten, static))
            if current == tcr:
                raise WriteRefused('%s is already on %s.' % (ten, tcr))
            moved = 0
            if current:
                _put_on_campaign(ten, '', webhook=info['webhook'])
            _put_on_campaign(ten, tcr, removed_already=moved)
        elif action == 'make_static':
            if static == tcr and current == tcr:
                raise WriteRefused('%s is already static on %s.' % (ten, tcr))
            if current != tcr:
                if current:
                    _put_on_campaign(ten, '', webhook=info['webhook'])
                _put_on_campaign(ten, tcr, lidb='STATIC', webhook=info['webhook'] or 'WebHook1')
            else:
                # already on it — just mark it so the CMS never moves it
                import time as _t
                _bv('POST', '/tnRecord', {'TN': '1' + ten, 'Lidb': 'STATIC',
                                          'Webhook': info['webhook'] or 'WebHook1', 'Tcr': tcr})
                _BV_ALL['records'] = None
                _t.sleep(0.5)
            _set_static_row(ten, tcr, True)
        elif action == 'unstatic':
            if not static:
                raise WriteRefused('%s is not static.' % ten)
            _set_static_row(ten, None, False)
            if current:
                # stays on its campaign, but now as a rotating number the CMS may move
                from datetime import datetime as _dt
                _bv('POST', '/tnRecord', {'TN': '1' + ten, 'Lidb': _dt.now().strftime('%Y%m%d%H%M'),
                                          'Webhook': info['webhook'] or 'WebHook2', 'Tcr': current})
                _BV_ALL['records'] = None
        after = number_info(ten)
    expected = {'off': None, 'on': tcr, 'make_static': tcr, 'unstatic': current}[action]
    confirmed = (after['campaign'] or None) == (expected or None)
    out = {'table': 'BulkVS', 'action': 'campaign_' + action, 'dry_run': False, 'ok': True,
           'committed': True, 'confirmed': confirmed, 'after': after,
           'values': {'number': ten, 'from': current, 'to': expected, 'reason': reason,
                      'static_before': static}}
    out['meaning'] = {
        'off': 'Took %s off %s.' % (ten, current),
        'on': 'Put %s on %s.' % (ten, tcr),
        'make_static': '%s is now static on %s — the CMS will never move it.' % (ten, tcr),
        'unstatic': '%s is no longer static. It stays on %s, but the CMS may now move it.'
                    % (ten, current or 'no campaign'),
    }[action]
    if not confirmed:
        out['meaning'] += (' BulkVS does not show the change yet (it shows %s) — check again in a '
                           'minute.' % (after['campaign'] or 'no campaign'))
    _log(who, out)
    return out



# ─── Saved logins and details (the old CMS's "Personal Info") ───────────────
def record_personal_info_view(info_id, employee_id, reason=None):
    """The old CMS's own record of every look — PersonalInfoMetadata, with the
    call and the work the agent had open at that moment — so its reports keep
    working whichever system the agent used."""
    if not employee_id:
        return None
    conn = cms_db._connect()
    try:
        cu = conn.cursor()
        cu.execute("""SELECT TOP 1 Id FROM AccountWork
                      WHERE EmployeeId = %s AND EndTime IS NULL ORDER BY Id DESC""", (int(employee_id),))
        w = cu.fetchone()
        cu.execute("SELECT Extension FROM Employee WHERE Id = %s", (int(employee_id),))
        e = cu.fetchone()
        call = None
        if e and e[0]:
            cu.execute("""SELECT TOP 1 Id FROM PhoneCallsLog
                          WHERE LTRIM(RTRIM(PickedUpBy)) = %s AND Ended IS NULL
                            AND Started >= DATEADD(hour, -6, GETDATE())
                          ORDER BY Id DESC""", (str(e[0]).strip(),))
            call = cu.fetchone()
        cu.execute('SET TRANSACTION ISOLATION LEVEL READ COMMITTED')
        cu.execute("""INSERT INTO PersonalInfoMetadata
                      (PersonalInfoId, Created, AccessedBy, Reason, PhoneCallId, AccountWorkId)
                      VALUES (%s, GETDATE(), %s, %s, %s, %s)""",
                   (int(info_id), int(employee_id), (reason or '')[:800] or None,
                    int(call[0]) if call else None, int(w[0]) if w else None))
        conn.commit()
        return {'call_id': int(call[0]) if call else None, 'work_id': int(w[0]) if w else None}
    finally:
        conn.close()


def _touch_account(cu, account_id):
    cu.execute('UPDATE Account SET Modified = GETDATE() WHERE Id = %s', (int(account_id),))


def add_personal_info(account_id, title, value, who):
    title = (title or '').strip()
    value = (value or '').strip()
    if not title or not value:
        raise WriteRefused('Give it a title and the details.')
    if len(title) > 250:
        raise WriteRefused('The title is too long (limit 250 characters).')
    if len(value) > MAX_NOTE:
        raise WriteRefused('That is too long.')
    conn = cms_db._connect()
    try:
        cu = conn.cursor()
        cu.execute('SET TRANSACTION ISOLATION LEVEL READ COMMITTED')
        cu.execute("""INSERT INTO AccountPersonalInfo (AccountId, CreatedBy, Title, Description, Created)
                      OUTPUT INSERTED.Id VALUES (%s, %s, %s, %s, GETDATE())""",
                   (int(account_id), int((who or {}).get('employee_id') or 0) or None, title, value))
        new_id = int(cu.fetchone()[0])
        _touch_account(cu, account_id)
        conn.commit()
    finally:
        conn.close()
    out = {'table': 'AccountPersonalInfo', 'action': 'add_personal_info', 'row_id': new_id,
           'ok': True, 'committed': True, 'dry_run': False,
           'values': {'account_id': int(account_id), 'title': title}}
    _log(who, out)
    return out


def edit_personal_info(info_id, title, value, who):
    title = (title or '').strip()
    value = (value or '').strip()
    if not title or not value:
        raise WriteRefused('Give it a title and the details.')
    if len(title) > 250:
        raise WriteRefused('The title is too long (limit 250 characters).')
    before = cms_db.personal_info(info_id)
    conn = cms_db._connect()
    try:
        cu = conn.cursor()
        cu.execute('SET TRANSACTION ISOLATION LEVEL READ COMMITTED')
        cu.execute("""UPDATE AccountPersonalInfo SET Title = %s, Description = %s, Modified = GETDATE()
                      WHERE Id = %s""", (title, value, int(info_id)))
        _touch_account(cu, before['account_id'])
        conn.commit()
    finally:
        conn.close()
    # the log keeps the title change, never the old or new secret itself
    out = {'table': 'AccountPersonalInfo', 'action': 'edit_personal_info', 'row_id': int(info_id),
           'ok': True, 'committed': True, 'dry_run': False,
           'values': {'account_id': before['account_id'], 'title': title,
                      'title_before': before['title'], 'value_changed': before['value'] != value}}
    _log(who, out)
    return out


def delete_personal_info(info_id, who):
    before = cms_db.personal_info(info_id)
    conn = cms_db._connect()
    try:
        cu = conn.cursor()
        cu.execute('SET TRANSACTION ISOLATION LEVEL READ COMMITTED')
        cu.execute('DELETE FROM AccountPersonalInfo WHERE Id = %s', (int(info_id),))
        _touch_account(cu, before['account_id'])
        conn.commit()
    finally:
        conn.close()
    out = {'table': 'AccountPersonalInfo', 'action': 'delete_personal_info', 'row_id': int(info_id),
           'ok': True, 'committed': True, 'dry_run': False,
           'values': {'account_id': before['account_id'], 'title': before['title']}}
    _log(who, out)
    return out
