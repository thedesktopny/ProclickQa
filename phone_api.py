"""The phone system's call reports — a copy of the old CMS's PhoneSystemController.

The phone server (proclick/agi/Api.class.php) POSTs every call event to
<apiUrl>PhoneSystem/<Action>, form-encoded (the same fields also in the query
string). The old CMS (proclick.azurewebsites.net/api/) wrote them into
PhoneCallsLog; StartCall also answers with the agents to ring, the caller's
accounts and the last agent. This module does exactly that against the same
CMS database, so the phone server's apiUrl can point at
https://cms.myhellodesk.com/api/ instead.

Ported from ZohoAccounts.Api.PhoneSystemController + Models.Repository
(decompiled, Nov 2026). Each function says which old method it copies.
Times are New York time, like the old CMS (DateTime.Now on its server).
"""
import json
import threading

import cms_db
from cms_write import _now_local, _with_new_id

MISSED_CODES = ('CHANUNAVAIL', 'CONGESTION', 'BUSY', 'NOANSWER', 'CANCEL', 'HANGUP', 'NO_AGENT_AVAIL')

# PhoneCallsLog columns the phone system may send (Web API bound them by name)
TEXT_FIELDS = ('AstriskUniqId', 'Phone', 'DialStatus', 'PickedUpBy', 'RecordingFileUrl', 'VoiceMail',
               'Note', 'Agent', 'CallTitle', 'CalledExtension', 'SpecificExten', 'HungUpBy',
               'CallersName', 'CustomerAskedExten', 'RoutedTo', 'AsteriskStateInfo',
               'AgentsStatusInfo', 'PreselectedAccountPhone', 'AccountToUse', 'Gender',
               'PreselectedAgent', 'RecordingScreenURL', 'RecordingCamURL')
BOOL_FIELDS = ('NewClient', 'RequestedCallBack', 'IsMissed', 'IsOutbound')


def is_missed(code):
    """PhoneSystemCodesHelper.IsMissedCall"""
    return str(code or '').upper().strip() in MISSED_CODES


def clean_phone(p):
    """Utilities.CleanPhoneNumber — the last 10 characters."""
    p = str(p or '')
    return p[-10:] if len(p) >= 10 else p


def _get(d, name):
    """Case-insensitive field lookup, like Web API model binding."""
    if name in d:
        return d[name]
    low = name.lower()
    for k, v in d.items():
        if k.lower() == low:
            return v
    return None


def _str(d, name):
    v = _get(d, name)
    if v is None:
        return None
    v = str(v)
    return v if v != '' else None


def _bool(d, name):
    v = _get(d, name)
    if v is None or v == '':
        return None
    return str(v).strip().lower() in ('1', 'true', 'yes', 'on')


def _rows(cu):
    cols = [c[0] for c in cu.description] if cu.description else []
    return [dict(zip(cols, r)) for r in cu.fetchall()]


def _one(cu):
    r = _rows(cu)
    return r[0] if r else None


def _call_by_uid(cu, uid):
    cu.execute('SELECT TOP 1 * FROM PhoneCallsLog WHERE AstriskUniqId = %s ORDER BY Id', (uid,))
    return _one(cu)


# ── helpers copied from Repository ──────────────────────────────────────────

def _handle_old_live_call(cu, call_id, agent, picked_up_by, now):
    """Repository.HandleOldLiveCall — an agent starting/answering a new call
    closes their previous call that was never ended (no transfers on it)."""
    ext = (agent or '') + (picked_up_by or '')
    if not ext:
        return
    cu.execute("""SELECT TOP 1 c.Id, c.DialStatus FROM PhoneCallsLog c
                  WHERE c.Agent = %s AND c.Ended IS NULL AND c.Id <> %s
                    AND NOT EXISTS (SELECT 1 FROM CallTransferLog t WHERE t.CallId = c.Id)""",
               (ext, int(call_id or 0)))
    r = cu.fetchone()
    if not r:
        return
    ds = (r[1] or '').split(' - ')[0]
    cu.execute("""UPDATE PhoneCallsLog SET Ended = %s, Modified = %s, DialStatus = %s,
                         Note = LEFT(ISNULL(Note,'') + ' - call was set as ended by the system after the agent was on a new call and this one was still open', 4000)
                  WHERE Id = %s""", (now, now, ds, int(r[0])))


def _handle_missed_resolved(cu, phone, call_id, extension, now):
    """Repository.HandleMissedCallsResolved — a call with this customer marks
    their open missed calls (to the agent's companies) as reached."""
    try:
        p10 = clean_phone(phone)
        if not p10 or not extension:
            return
        cu.execute("""UPDATE m SET MissedResolvedId = %s, Modified = %s
                      FROM PhoneCallsLog m
                      WHERE (m.IsMissed = 1 OR m.DialStatus = 'AFTER HOURS')
                        AND RIGHT(m.Phone, 10) = %s
                        AND (m.MissedResolvedId IS NULL OR m.MissedResolvedId = 0)
                        AND EXISTS (SELECT 1 FROM TableCompanies co
                                    JOIN EmployeeCompanyRelations r ON r.CompanyId = co.Id
                                    JOIN Employee e ON e.Id = r.EmployeeId
                                    WHERE e.Extension = %s
                                      AND co.PhoneNumbers LIKE '%%' + m.CalledExtension + '%%')""",
                   (int(call_id), now, p10, str(extension)))
    except Exception as e:
        print('[phone-api] missed-resolved: ' + str(e)[:160], flush=True)


def _queue_info(cu, uid, info, extension, now):
    """Repository.UpdateQueueCall"""
    call = _call_by_uid(cu, uid)
    if not call:
        return False
    info = info or ''
    cu.execute('INSERT INTO QueueInfo (CallLogId, Created, Info) VALUES (%s, %s, %s)',
               (call['Id'], now, info))
    sets, args = ["Modified = %s", "DialStatus = 'QUEUE'"], [now]
    if info.lower().startswith('calling'):
        sets.append('SpecificExten = %s')
        args.append(info.strip()[-3:])
    if extension:
        sets.append('PreselectedAgent = %s')
        args.append(extension)
    cu.execute('UPDATE PhoneCallsLog SET ' + ', '.join(sets) + ' WHERE Id = %s', args + [call['Id']])
    return True


def _account_asterisk(a):
    """AccountAsterisk"""
    if not a:
        return None
    return {'Id': a.get('Id'), 'ContactID': a.get('ContactID'), 'FirstName': a.get('FirstName'),
            'LastName': a.get('LastName'), 'Phone': a.get('Phone'), 'HomePhone': a.get('HomePhone'),
            'OtherPhone': a.get('OtherPhone'), 'Fax': a.get('Fax'), 'Mobile': a.get('Mobile'),
            'MinutesLeft': a.get('MinutesLeft')}


_ACC_COLS = 'Id, ContactID, FirstName, LastName, Phone, HomePhone, OtherPhone, Fax, Mobile, MinutesLeft, Deleted, CreatedTime'


def _account_by_phone(cu, phone):
    """Repository.GetAccountByPhone"""
    try:
        p = str(phone or '').split(',')[0][-10:]
        if len(p) < 10:
            return None
        cu.execute('SELECT ' + _ACC_COLS + """ FROM Account
                      WHERE RIGHT(Phone,10) = %s OR RIGHT(HomePhone,10) = %s OR RIGHT(Mobile,10) = %s""",
                   (p, p, p))
        acc = _rows(cu)
        if not acc:
            return None
        live = [a for a in acc if not a.get('Deleted')]
        if len(acc) > 1 and live:
            if len(live) == 1:
                return live[0]
            return sorted(live, key=lambda a: a.get('CreatedTime') or 0, reverse=True)[0]
        return sorted(acc, key=lambda a: a.get('CreatedTime') or 0, reverse=True)[0]
    except Exception:
        return None


def _find_accounts(cu, phone):
    """Repository.FindAccountByPhoneAsterisk"""
    raw = str(phone or '')
    associated = []
    if len(raw) >= 10:
        cu.execute('SELECT AccountId FROM AccountAssociatedPhoneNumber WHERE RIGHT(Phone,10) = %s',
                   (raw[-10:],))
        for (aid,) in cu.fetchall():
            cu.execute('SELECT ' + _ACC_COLS + ' FROM Account WHERE Id = %s', (aid,))
            a = _one(cu)
            if a:
                associated.append(_account_asterisk(a))
    p = clean_phone(raw)
    others = []
    if p:
        like = '%' + p + '%'
        cu.execute('SELECT ' + _ACC_COLS + ' FROM Account WHERE Mobile LIKE %s OR OtherPhone LIKE %s OR HomePhone LIKE %s',
                   (like, like, like))
        others = [_account_asterisk(a) for a in _rows(cu)]
    return {'Primary': _account_asterisk(_account_by_phone(cu, p)),
            'Others': others, 'Associated': associated}


def _extensions(cu, called):
    """Repository.GetEmployeesExtensionsAsterisk — the CMS procedure
    ProcGetExtensions decides who rings for this called number."""
    try:
        cu.execute('EXEC dbo.ProcGetExtensions %s', (called,))
        out = []
        for r in _rows(cu):
            low = {k.lower(): v for k, v in r.items()}
            out.append({'Extension': low.get('extension'), 'Gender': low.get('gender'),
                        'ExperienceLevel': low.get('experiencelevel')})
        return out
    except Exception as e:
        print('[phone-api] ProcGetExtensions: ' + str(e)[:160], flush=True)
        return None


def _last_agent(cu, phone):
    """Repository.GetLastCall(phone, onlyPickedUp: true)"""
    if not phone:
        return ''
    cu.execute("""SELECT TOP 1 PickedUpBy, Agent FROM PhoneCallsLog
                  WHERE Phone LIKE %s AND (PickedUpBy IS NOT NULL OR Agent IS NOT NULL)
                  ORDER BY Modified DESC""", ('%' + str(phone) + '%',))
    r = cu.fetchone()
    if not r:
        return ''
    return (r[1] if r[0] is None else r[0]) or ''


def _stop_recorder(extension):
    """EmployeeServiceHelper.StopRecorderAsync — best effort, in the background."""
    def run():
        try:
            import urllib.request, urllib.parse
            conn = cms_db._connect(); cu = conn.cursor()
            cu.execute("""SELECT TOP 1 n.Url FROM Ngrok n JOIN Employee e ON e.Id = n.EmployeeId
                          WHERE e.Extension = %s AND ISNULL(n.Url,'') <> '' ORDER BY e.Id DESC""", (extension,))
            r = cu.fetchone(); conn.close()
            if r and r[0]:
                data = urllib.parse.urlencode({'command': 'recordingstop'}).encode()
                urllib.request.urlopen(urllib.request.Request(r[0], data=data), timeout=5).read()
        except Exception:
            pass
    threading.Thread(target=run, daemon=True).start()


# ── the actions ─────────────────────────────────────────────────────────────

def start_call(d):
    """PhoneSystemController.StartCall + Repository.SaveStartCall"""
    uid = _str(d, 'AstriskUniqId')
    if not uid:
        return None, 500
    now = _now_local()
    conn = cms_db._connect()
    try:
        cu = conn.cursor()
        vals = {f: _str(d, f) for f in TEXT_FIELDS}
        vals.update({f: _bool(d, f) for f in BOOL_FIELDS})
        vals = {k: v for k, v in vals.items() if v is not None}
        vals['Started'] = now
        vals['Modified'] = now
        if vals.get('Agent'):
            vals['DialStatus'] = 'OUTBOUND - NO PICKUP'
            vals['IsOutbound'] = True
        cols = list(vals)
        cu.execute('SET TRANSACTION ISOLATION LEVEL READ COMMITTED')
        cu.execute(_with_new_id('INSERT INTO PhoneCallsLog (' + ', '.join(cols) + ') OUTPUT INSERTED.Id VALUES ('
                                + ', '.join(['%s'] * len(cols)) + ')'), tuple(vals[c] for c in cols))
        r = cu.fetchone()
        new_id = int(r[0]) if r else None
        _handle_old_live_call(cu, new_id, vals.get('Agent'), vals.get('PickedUpBy'), now)
        conn.commit()
        if vals.get('IsOutbound') is False:
            # the old code: `if (log.IsOutbound == false)` — only when the phone
            # system says so explicitly (an unset value is not false in C#)
            _queue_info(cu, uid, 'START call', vals.get('PickedUpBy'), now)
        elif vals.get('IsOutbound') is True:
            _handle_missed_resolved(cu, vals.get('Phone'), new_id,
                                    vals.get('PickedUpBy') or vals.get('Agent'), now)
        conn.commit()
        # the answer: who to ring, the caller's accounts, the last agent
        called = clean_phone(vals.get('CalledExtension'))
        out = {'Extensions': _extensions(cu, called),
               'Accounts': _find_accounts(cu, vals.get('Phone')),
               'lastAgent': _last_agent(cu, vals.get('Phone'))}
        return out, 200
    except Exception as e:
        try:
            conn.rollback()
        except Exception:
            pass
        print('[phone-api] StartCall %s: %s' % (uid, str(e)[:200]), flush=True)
        return None, 500
    finally:
        conn.close()


def update_call(d):
    """Repository.SaveUpdateCall"""
    uid = _str(d, 'AstriskUniqId')
    now = _now_local()
    conn = cms_db._connect()
    try:
        cu = conn.cursor()
        call = _call_by_uid(cu, uid) if uid else None
        if not call:
            return None, 200
        atu = _str(d, 'AccountToUse')
        cu.execute("""UPDATE PhoneCallsLog SET Modified = %s, AccountToUse = %s, Gender = %s,
                             NewClient = %s, RoutedTo = %s, PreselectedAccountPhone = %s WHERE Id = %s""",
                   (now, atu, _str(d, 'Gender'), _bool(d, 'NewClient'), _str(d, 'RoutedTo'), atu, call['Id']))
        conn.commit()
        return None, 200
    except Exception as e:
        print('[phone-api] UpdateCall %s: %s' % (uid, str(e)[:200]), flush=True)
        return None, 500
    finally:
        conn.close()


def update_queue_call(d):
    """PhoneSystemController.UpdateQueueCall"""
    uid = _str(d, 'AstriskUniqId')
    conn = cms_db._connect()
    try:
        cu = conn.cursor()
        if uid:
            _queue_info(cu, uid, _str(d, 'Info') or '', _str(d, 'extension'), _now_local())
            conn.commit()
        return None, 200
    except Exception as e:
        print('[phone-api] UpdateQueueCall %s: %s' % (uid, str(e)[:200]), flush=True)
        return None, 500
    finally:
        conn.close()


def picked_up_call(d):
    """Repository.SaveCallPickedUp"""
    uid = _str(d, 'AstriskUniqId')
    now = _now_local()
    conn = cms_db._connect()
    try:
        cu = conn.cursor()
        call = _call_by_uid(cu, uid) if uid else None
        if not call:
            return None, 500
        pub = _str(d, 'PickedUpBy')
        cu.execute("""UPDATE PhoneCallsLog SET PickedUpBy = %s, DialStatus = %s, PickedUpTime = %s,
                             Modified = %s, RecordingFileUrl = %s WHERE Id = %s""",
                   (pub, _str(d, 'DialStatus'), now, now, _str(d, 'RecordingFileUrl'), call['Id']))
        _handle_old_live_call(cu, call['Id'], call.get('Agent'), pub, now)
        conn.commit()
        _handle_missed_resolved(cu, call.get('Phone'), call['Id'], pub, now)
        conn.commit()
        return None, 200
    except Exception as e:
        print('[phone-api] PickedUpCall %s: %s' % (uid, str(e)[:200]), flush=True)
        return None, 500
    finally:
        conn.close()


def end_call(d):
    """Repository.SaveCallEnd (+ the recorder stop from EndCall)"""
    uid = _str(d, 'AstriskUniqId') or ''
    now = _now_local()
    conn = cms_db._connect()
    try:
        cu = conn.cursor()
        call = _call_by_uid(cu, uid) if uid else None
        if not call:
            before = uid.split('.')[0]
            cu.execute("""SELECT TOP 1 * FROM PhoneCallsLog c
                          WHERE c.AstriskUniqId LIKE %s
                            AND EXISTS (SELECT 1 FROM CallTransferLog t WHERE t.CallId = c.Id)
                          ORDER BY c.Id DESC""", ('%' + before + '%',))
            call = _one(cu)
            if not call:
                return {'message': 'was an exception, probably a missed call from a outbound? '
                                   'The asterisk unique id could not be found'}, 200
        ds = _str(d, 'DialStatus')
        sets = {'Ended': now, 'Modified': now, 'DialStatus': ds, 'HungUpBy': _str(d, 'HungUpBy'),
                'CustomerAskedExten': _str(d, 'CustomerAskedExten'),
                'AsteriskStateInfo': _str(d, 'AsteriskStateInfo'),
                'AgentsStatusInfo': _str(d, 'AgentsStatusInfo')}
        missed = None
        if call.get('Agent'):
            sets['DialStatus'] = 'OUTBOUND' if ds == 'ANSWER' else 'OUTBOUND ' + (ds or '')
        else:
            missed = is_missed(ds)
            sets['IsMissed'] = missed
        if missed is not True:
            cu.execute('SELECT COUNT(*) FROM AccountWork WHERE PhoneCallId = %s', (call['Id'],))
            if not cu.fetchone()[0]:
                sets['IsAwaitingEmployeeInput'] = True
        cols = list(sets)
        cu.execute('UPDATE PhoneCallsLog SET ' + ', '.join(c + ' = %s' for c in cols) + ' WHERE Id = %s',
                   tuple(sets[c] for c in cols) + (call['Id'],))
        try:
            cu.execute("""UPDATE CustomerCredentialData SET AwaitingCustomerInput = 0
                          WHERE PhoneCallId = %s AND AwaitingCustomerInput = 1""", (call['Id'],))
        except Exception:
            pass
        conn.commit()
        if not is_missed(ds):
            cu.execute('SELECT COUNT(*) FROM AccountWork WHERE PhoneCallId = %s AND EndTime IS NULL', (call['Id'],))
            if not cu.fetchone()[0]:
                _stop_recorder((call.get('PickedUpBy') or '') + (call.get('Agent') or ''))
        return None, 200
    except Exception as e:
        print('[phone-api] EndCall %s: %s' % (uid, str(e)[:200]), flush=True)
        return {'message': 'was an exception, probably a missed call from a outbound? ' + str(e)[:200]}, 200
    finally:
        conn.close()


def voicemail(d):
    """Repository.SaveVoicemail"""
    uid = _str(d, 'AstriskUniqId')
    conn = cms_db._connect()
    try:
        cu = conn.cursor()
        call = _call_by_uid(cu, uid) if uid else None
        if not call:
            return None, 500
        cu.execute('UPDATE PhoneCallsLog SET VoiceMail = %s, Modified = %s WHERE Id = %s',
                   (_str(d, 'VoiceMail'), _now_local(), call['Id']))
        conn.commit()
        return None, 200
    finally:
        conn.close()


def is_blocked_number(d):
    """PhoneSystemController.IsBlockedNumber"""
    n = str(_str(d, 'number') or '')
    conn = cms_db._connect()
    try:
        cu = conn.cursor()
        cu.execute('SELECT COUNT(*) FROM BlockedNumbers WHERE RIGHT(Number,10) = %s', (n[-10:],))
        return {'isBlocked': bool(cu.fetchone()[0])}, 200
    finally:
        conn.close()


ACTIONS = {
    'startcall': start_call, 'callstart': start_call,
    'updatecall': update_call,
    'updatequeuecall': update_queue_call,
    'pickedupcall': picked_up_call, 'callpickedup': picked_up_call,
    'endcall': end_call, 'callend': end_call,
    'voicemail': voicemail,
    'isblockednumber': is_blocked_number,
}
