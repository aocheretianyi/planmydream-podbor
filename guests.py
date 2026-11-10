"""«Кто уже едет»: обезличенный состав оплаченных групп из amoCRM для подборщика.

Берём сделки «Оплата произведена» / «Успешно реализовано» с будущей «Датой начала тура»
и «Подобранным туром» (ссылка на planmydream.ru). По каждому туру и дате начала — группы «кто с кем»:
только пол (по имени/отчеству) и возраст на дату тура. Имён, фамилий, дат рождения, телефонов
в результате НЕТ, и в лог они не печатаются.
Токен — переменная окружения AMO_TOKEN (секрет GitHub). Без токена оставляем прошлые данные.
"""
import json, os, re, urllib.request
from datetime import datetime, timedelta, timezone

BASE = 'https://ocheretianyianton.amocrm.ru'
STAGES = [(9926974, 89201958), (9926974, 142), (11081078, 142)]  # Первичные: оплата / успешно; Повторные: успешно
F_START, F_TOUR, F_PEOPLE, F_BIRTHDAY = 1499639, 1497501, 879401, 1495751
MSK = timezone(timedelta(hours=3))

MALE_A = re.compile(r'^(никита|илья|кузьма|фома|лука|савва|данила|гаврила|миша|паша|дима|коля|вова|ваня|петя|'
                    r'серёжа|сережа|лёша|леша|гоша|гриша|костя|слава|стёпа|степа|юра|тёма|тема)$', re.I)


def _get(path, token):
    req = urllib.request.Request(BASE + path, headers={'Authorization': 'Bearer ' + token, 'User-Agent': 'podbor'})
    with urllib.request.urlopen(req, timeout=60) as r:
        body = r.read()
    return json.loads(body) if body else {}  # 204 — пусто


def _cf(o, fid):
    for f in o.get('custom_fields_values') or []:
        if f.get('field_id') == fid and f.get('values'):
            return f['values'][0].get('value')
    return None


def _ts(v):
    s = str(v)
    if re.fullmatch(r'-?\d+', s):
        return int(s)  # до 1970 — отрицательное
    return datetime.fromisoformat(s.replace('Z', '+00:00')).timestamp()


def _msk(v):
    return datetime.fromtimestamp(_ts(v), MSK)


def gender(c):
    full = f"{c.get('name') or ''} {c.get('first_name') or ''}"
    if re.search(r'вна\b|кызы\b', full, re.I):
        return 'Ж'
    if re.search(r'вич\b|ьич\b|оглы\b', full, re.I):
        return 'М'
    first = (str(c.get('first_name') or c.get('name') or '').strip().split() or [''])[0]
    if not first or re.match(r'[A-Za-z]', first) or re.fullmatch(r'(женя|валя|саша)', first, re.I):
        return '?'  # латиница и общие имена — пол не угадываем
    if MALE_A.match(first):
        return 'М'
    return 'Ж' if re.search(r'[ая]$', first, re.I) else 'М'


def build(leads, contacts, now):
    out, skipped = {}, 0
    for l in leads:
        start, tour = _cf(l, F_START), _cf(l, F_TOUR)
        if not start or not tour or not str(tour).startswith('http') or _ts(start) < now - 86400:
            skipped += 1
            continue
        slug = re.sub(r'[?#].*$', '', str(tour)).rstrip('/').split('/')[-1]
        s = _msk(start)
        people = []
        for ref in (l.get('_embedded') or {}).get('contacts') or []:
            c = contacts.get(ref['id'], {})
            bd, age = _cf(c, F_BIRTHDAY), None
            if bd:
                b = _msk(bd)
                age = s.year - b.year - ((s.month, s.day) < (b.month, b.day))
            people.append({'g': gender(c), 'age': age})
        try:
            declared = int(_cf(l, F_PEOPLE) or 0)
        except (TypeError, ValueError):
            declared = 0
        group = {'people': people, 'unknown': max(0, declared - len(people))}
        out.setdefault(f"{slug}|{s.date().isoformat()}", []).append(group)
    return out, skipped


def collect(prev, warnings):
    token = os.environ.get('AMO_TOKEN')
    if not token:
        warnings.append('«Кто уже едет»: нет токена AMO_TOKEN — показаны прошлые данные')
        return prev
    try:
        leads = []
        for pipe, status in STAGES:
            for page in range(1, 20):
                r = _get(f'/api/v4/leads?limit=250&page={page}&with=contacts'
                         f'&filter[statuses][0][pipeline_id]={pipe}&filter[statuses][0][status_id]={status}', token)
                got = (r.get('_embedded') or {}).get('leads') or []
                # amoCRM молча игнорирует кривой фильтр — оставляем только нужный этап
                leads += [l for l in got if l['pipeline_id'] == pipe and l['status_id'] == status]
                if len(got) < 250:
                    break
        ids = sorted({c['id'] for l in leads for c in (l.get('_embedded') or {}).get('contacts') or []})
        contacts = {}
        for i in range(0, len(ids), 200):
            q = '&'.join(f'filter[id][]={x}' for x in ids[i:i + 200])
            r = _get(f'/api/v4/contacts?limit=250&{q}', token)
            for c in (r.get('_embedded') or {}).get('contacts') or []:
                contacts[c['id']] = c
        groups, skipped = build(leads, contacts, datetime.now(timezone.utc).timestamp())
        print(f'  «Кто уже едет»: {len(leads)} оплаченных сделок, {len(groups)} заездов, без даты/тура или прошедших: {skipped}')
        return {'at': datetime.now(timezone.utc).isoformat(timespec='seconds'), 'groups': groups}
    except Exception as e:  # сбой amoCRM не должен ронять сборку
        warnings.append(f'«Кто уже едет»: amoCRM не ответил ({type(e).__name__}) — показаны прошлые данные')
        return prev
