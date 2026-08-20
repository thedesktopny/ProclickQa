"""
Phone availability — collector and calculator.

Igor's endpoint returns SNAPSHOTS, not events: one row per agent per moment,
saying whether the phone was online and what CMS state they were in. We store
those snapshots ourselves because the API only serves a 7-day window, so history
exists only if we keep it.

Availability for a shift is then: of the snapshots inside that shift, what share
show the agent both phone-online and CMS-active. Because snapshots arrive at a
fixed interval, "share of snapshots" converts directly into minutes.

    GET https://panel.myhellodesk.com/api/agents_phone_status
        header: api-key: <token>
        from / to: 'YYYY-MM-DD' or 'YYYY-MM-DD HH:MM:SS', max 7 days apart
        server timezone: America/New_York

    row: {"tm","agent","EmployeeId","phone_status","cms_status"}
         phone_status  0 offline, 1 online
         cms_status    0 not active, 1 active, 2 break, 3 DND
"""
import os
import json
import urllib.parse
import urllib.request
from datetime import datetime, timedelta

API_URL = os.getenv('PHONE_API_URL', 'https://panel.myhellodesk.com/api/agents_phone_status')
API_KEY = os.getenv('PHONE_API_KEY', '')
MAX_RANGE_DAYS = 7          # the API's own limit
SERVER_TZ = 'America/New_York'

CMS_LABELS = {0: 'not active', 1: 'active', 2: 'break', 3: 'DND'}


def configured():
    return bool(API_KEY)


def fetch(date_from, date_to, timeout=60):
    """One call to Igor's API. Times are the phone system's own timezone."""
    if not API_KEY:
        raise RuntimeError('PHONE_API_KEY is not set')
    qs = urllib.parse.urlencode({'from': date_from, 'to': date_to})
    req = urllib.request.Request(API_URL + '?' + qs, headers={
        'api-key': API_KEY,
        'Accept': 'application/json',
        'User-Agent': 'VoiceGuard/1.0',
    })
    with urllib.request.urlopen(req, timeout=timeout) as r:
        body = r.read().decode('utf-8', 'replace')
    data = json.loads(body)
    if isinstance(data, dict):                 # tolerate {"data": [...]} shapes
        for key in ('data', 'rows', 'result', 'items'):
            if isinstance(data.get(key), list):
                data = data[key]
                break
    if not isinstance(data, list):
        raise RuntimeError('unexpected response shape: ' + body[:160])
    return data


def chunks(date_from, date_to, days=MAX_RANGE_DAYS):
    """Split a range into pieces the API will accept."""
    a = datetime.strptime(date_from[:10], '%Y-%m-%d').date()
    b = datetime.strptime(date_to[:10], '%Y-%m-%d').date()
    out = []
    while a <= b:
        end = min(b, a + timedelta(days=days - 1))
        out.append((a.isoformat(), end.isoformat()))
        a = end + timedelta(days=1)
    return out


def store(conn, rows):
    """Save snapshots. Re-running the same range is safe — duplicates are ignored."""
    c = conn.cursor()
    saved = 0
    for r in rows:
        try:
            tm = str(r.get('tm') or '').strip()
            if not tm:
                continue
            emp = r.get('EmployeeId')
            emp = int(emp) if emp not in (None, '') else None
            c.execute("""INSERT INTO phone_status_snapshots
                            (snapshot_at, agent_ext, employee_id, phone_status, cms_status)
                         VALUES (%s, %s, %s, %s, %s)
                         ON CONFLICT (snapshot_at, agent_ext) DO NOTHING""",
                      (tm, str(r.get('agent') or '').strip(), emp,
                       int(r.get('phone_status') or 0), int(r.get('cms_status') or 0)))
            saved += c.rowcount
        except Exception as e:
            print('[phone_status] skipped a row: ' + str(e)[:120])
            continue
    conn.commit()
    return saved


def collect(conn, date_from, date_to):
    """Fetch a range (splitting it as needed) and store it."""
    total_rows, total_saved, errors = 0, 0, []
    for a, b in chunks(date_from, date_to):
        try:
            rows = fetch(f'{a} 00:00:00', f'{b} 23:59:59')
            total_rows += len(rows)
            total_saved += store(conn, rows)
        except Exception as e:
            errors.append(f'{a}..{b}: {str(e)[:140]}')
    return {'fetched': total_rows, 'stored': total_saved, 'errors': errors}


def detect_interval(conn, employee_id=None):
    """How often snapshots arrive, measured from what we actually stored.
    Availability is 'share of snapshots x interval', so this number decides how
    precise the answer is — and whether it deserves to be called exact."""
    c = conn.cursor()
    c.execute("""SELECT snapshot_at FROM phone_status_snapshots
                 WHERE (%s IS NULL OR employee_id = %s)
                 ORDER BY snapshot_at DESC LIMIT 400""",
              (employee_id, employee_id))
    times = sorted({r[0] for r in c.fetchall()})
    if len(times) < 3:
        return None
    gaps = []
    for i in range(1, len(times)):
        g = (times[i] - times[i-1]).total_seconds()
        if 1 <= g <= 3600:
            gaps.append(g)
    if not gaps:
        return None
    gaps.sort()
    return gaps[len(gaps)//2]          # median, so a gap in collection doesn't skew it


def availability_for(conn, employee_id, start, end, interval_seconds=None):
    """Availability inside one shift.

    Returns minutes on the phone and ready (phone online AND cms active), plus
    the break/DND/offline split so a manager can see WHY someone wasn't
    available, not just that they weren't.
    """
    if employee_id is None or start is None or end is None:
        return None
    c = conn.cursor()
    c.execute("""SELECT phone_status, cms_status FROM phone_status_snapshots
                 WHERE employee_id = %s AND snapshot_at >= %s AND snapshot_at < %s""",
              (employee_id, start, end))
    rows = c.fetchall()
    if not rows:
        return None                      # no data — say nothing rather than imply zero

    step = (interval_seconds or detect_interval(conn, employee_id) or 60) / 60.0
    counts = {'available': 0, 'break': 0, 'dnd': 0, 'inactive': 0, 'phone_offline': 0}
    for phone, cms in rows:
        if not phone:
            counts['phone_offline'] += 1
        elif cms == 1:
            counts['available'] += 1
        elif cms == 2:
            counts['break'] += 1
        elif cms == 3:
            counts['dnd'] += 1
        else:
            counts['inactive'] += 1

    total = len(rows)
    shift_minutes = max(0.0, (end - start).total_seconds() / 60.0)
    covered = total * step
    return {
        'available_minutes': round(counts['available'] * step, 1),
        'break_minutes': round(counts['break'] * step, 1),
        'dnd_minutes': round(counts['dnd'] * step, 1),
        'inactive_minutes': round(counts['inactive'] * step, 1),
        'offline_minutes': round(counts['phone_offline'] * step, 1),
        'available_pct': round(counts['available'] / total * 100, 1) if total else None,
        'samples': total,
        'interval_seconds': round(step * 60),
        # How much of the shift we actually have data for. Anything well under
        # 100% means the collector missed a stretch, and the figures should be
        # read as partial rather than as a low score for the agent.
        'coverage_pct': round(min(100.0, covered / shift_minutes * 100), 1) if shift_minutes else None,
    }
