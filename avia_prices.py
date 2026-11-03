#!/usr/bin/env python3
"""Цены на авиабилеты для вкладки «✈ Билеты» → avia_prices.json.

Берёт открытые заезды из data.json, для каждого тура — город прилёта и вылета в Китае (как во вкладке:
по маршруту и FAQ «Прилёт в … / вылет из …»), и для основных городов России спрашивает у Travelpayouts
(кэш цен Aviasales, Data API v3 prices_for_dates) самые дешёвые билеты в одну сторону на месяц.
Сохраняет минимальную цену на нужные даты: туда — день начала и накануне, обратно — день окончания.

Токен — только из переменной окружения TRAVELPAYOUTS_TOKEN (секрет GitHub), в файлы и логи не попадает.
Запуск: GitHub Actions .github/workflows/avia.yml, раз в день.
"""
import datetime as dt
import json
import os
import re
import sys
import time
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

HERE = Path(__file__).resolve().parent
OUT = HERE / 'avia_prices.json'
API = 'https://api.travelpayouts.com/aviasales/v3/prices_for_dates'


def origins_from_page():
    """Города вылета — те же, что в списке вкладки «✈ Билеты» (RU_AIR в index.html), чтобы списки не расходились."""
    src = (HERE / 'index.html').read_text(encoding='utf-8')
    block = src[src.index('const RU_AIR'):src.index('};', src.index('const RU_AIR'))]
    return re.findall(r"'[^']+':'([A-Z]{3})'", block)


ORIGINS = origins_from_page()

# те же справочники, что во вкладке (index.html)
CN_AIR = {'Пекин': 'BJS', 'Шанхай': 'SHA', 'Гуанчжоу': 'CAN', 'Шэньчжэнь': 'SZX', 'Гонконг': 'HKG', 'Чунцин': 'CKG', 'Чэнду': 'CTU',
          'Сиань': 'XIY', 'Чжанцзяцзе': 'DYG', 'Гуйлинь': 'KWL', 'Ханчжоу': 'HGH', 'Хуаншань': 'TXN', 'Куньмин': 'KMG', 'Дали': 'DLU',
          'Лицзян': 'LJG', 'Санья': 'SYX', 'Хайкоу': 'HAK', 'Харбин': 'HRB', 'Далянь': 'DLC', 'Циндао': 'TAO', 'Сямынь': 'XMN',
          'Нанкин': 'NKG', 'Ухань': 'WUH', 'Чанша': 'CSX', 'Урумчи': 'URC'}
CN_NEAR = {'Чжуцзяцзяо': 'Шанхай', 'Учжэнь': 'Шанхай', 'Сучжоу': 'Шанхай', 'Яншо': 'Гуйлинь', 'Фуронг': 'Чжанцзяцзе', 'Фэнхуан': 'Чжанцзяцзе',
           'Хуанлун': 'Чэнду', 'Цзючжайгоу': 'Чэнду', 'Хуашань': 'Сиань', 'Шангри-ла': 'Лицзян', 'Вансянгу': 'Хуаншань',
           'Шэнсянцзю': 'Ханчжоу', 'Улинъюань': 'Чжанцзяцзе'}


def by_stem(w):
    return next((c for c in CN_AIR if w and w[:5] == c[:5]), None)


def tour_airports(t):
    route = [s.strip() for s in re.split(r'\s+—\s+', t.get('route', '')) if s.strip()]
    pick = lambda c: c if c in CN_AIR else CN_NEAR.get(c) or by_stem(c)
    arr, dep = pick(route[0]) if route else None, pick(route[-1]) if route else None
    faq = '\n'.join(f"{o.get('q', '')} {o.get('a', '')}" for o in t.get('faq') or [])
    a = re.search(r'Прил[её]т\s*(?:[—–-]\s*)?в\s+([А-ЯЁ][а-яё]+)', faq)
    d = re.search(r'[Вв]ылет\s*(?:[—–-]\s*)?из\s+([А-ЯЁ][а-яё]+)', faq)
    if a and by_stem(a.group(1)):
        arr = by_stem(a.group(1))
    if d and by_stem(d.group(1)):
        dep = by_stem(d.group(1))
    return CN_AIR.get(arr), CN_AIR.get(dep)


# Часовые пояса городов (UTC+ч) — чтобы показать прилёт по местному времени. Китай — UTC+8.
TZ = {'MOW': 3, 'LED': 3, 'KZN': 3, 'AER': 3, 'KRR': 3, 'ROV': 3, 'VOG': 3, 'GOJ': 3, 'MRV': 3, 'KUF': 4, 'IJK': 4, 'RTW': 4,
      'SVX': 5, 'UFA': 5, 'PEE': 5, 'CEK': 5, 'TJM': 5, 'SGC': 5, 'REN': 5, 'NQZ': 5, 'OMS': 6, 'OVB': 7, 'KJA': 7, 'KEJ': 7,
      'TOF': 7, 'BAX': 7, 'NOZ': 7, 'IKT': 8, 'UUD': 8, 'HTA': 9, 'YKS': 9, 'VVO': 10, 'KHV': 10, 'PKC': 12}


def ticket(x, dest):
    """Короткая запись билета: цена, авиакомпания+рейс, вылет/прилёт местного времени, аэропорты, пересадки, ссылка."""
    rec = {'p': x['price'], 'a': x.get('airline', ''), 't': x.get('transfers', 0)}
    mins = x.get('duration_to') or x.get('duration') or 0
    rec['h'] = round(mins / 60, 1)
    if x.get('flight_number'):
        rec['f'] = f"{x.get('airline', '')} {x['flight_number']}".strip()
    if x.get('origin_airport'):
        rec['oa'] = x['origin_airport']
    if x.get('destination_airport'):
        rec['da'] = x['destination_airport']
    try:
        dep = dt.datetime.fromisoformat(x['departure_at'])
        rec['d'] = dep.strftime('%H:%M')
        if mins and dep.tzinfo:
            tz = dt.timezone(dt.timedelta(hours=TZ.get(dest, 8)))   # всё, что не в справочнике России, — Китай (UTC+8)
            arr = (dep + dt.timedelta(minutes=mins)).astimezone(tz)
            days = (arr.date() - dep.date()).days
            rec['r'] = arr.strftime('%H:%M') + (f' +{days}' if days > 0 else '')
    except Exception:
        pass
    if x.get('link'):
        rec['l'] = x['link']
    return rec


MODE = {'how': 'header'}   # как передавать токен: заголовком X-Access-Token или параметром token


def call(token, q):
    """Запрос к API. Не принят заголовок — пробуем параметр; оба 401/403 — выходим с текстом ошибки (без токена)."""
    def get(how):
        if how == 'header':
            req = urllib.request.Request(f'{API}?{q}', headers={'X-Access-Token': token})
        else:
            req = urllib.request.Request(f'{API}?{q}&token={urllib.parse.quote(token)}')
        with urllib.request.urlopen(req, timeout=60) as r:
            return json.loads(r.read().decode('utf-8'))
    try:
        return get(MODE['how'])
    except urllib.error.HTTPError as e:
        if e.code not in (401, 403) or MODE['how'] == 'param':
            if e.code in (401, 403):
                body = e.read().decode('utf-8', 'ignore')[:300].replace(token, '***')
                sys.exit(f'Travelpayouts не принял токен (HTTP {e.code}: {body}). Длина токена: {len(token)} (ожидается 32)')
            raise
        body = e.read().decode('utf-8', 'ignore')[:300].replace(token, '***')
        try:
            data = get('param')
            MODE['how'] = 'param'
            print('заголовок не принят, токен параметром — принят')
            return data
        except urllib.error.HTTPError as e2:
            body2 = e2.read().decode('utf-8', 'ignore')[:300].replace(token, '***')
            sys.exit(f'Travelpayouts не принял токен (заголовком: HTTP {e.code} {body} | параметром: HTTP {e2.code} {body2}). '
                     f'Длина токена: {len(token)} (ожидается 32)')


def month_prices(token, origin, dest, month, cache):
    key = (origin, dest, month)
    if key in cache:
        return cache[key]
    q = urllib.parse.urlencode({'origin': origin, 'destination': dest, 'departure_at': month, 'one_way': 'true',
                                'currency': 'rub', 'sorting': 'price', 'direct': 'false', 'limit': 1000})
    best = {}
    for i in range(3):
        try:
            data = call(token, q)
            for x in data.get('data') or []:
                day = (x.get('departure_at') or '')[:10]
                if day and (day not in best or x['price'] < best[day]['p']):
                    best[day] = ticket(x, dest)
            break
        except SystemExit:
            raise
        except Exception:
            time.sleep(5 * (i + 1))
    time.sleep(0.4)
    cache[key] = best
    return best


def main():
    token = os.environ.get('TRAVELPAYOUTS_TOKEN', '').strip()
    if not token:
        sys.exit('Нет TRAVELPAYOUTS_TOKEN — цены не обновлены')
    tours = json.loads((HERE / 'data.json').read_text(encoding='utf-8'))['tours']
    today = dt.date.today()
    need = {}   # (origin, dest) -> set(dates)
    for t in tours:
        A, D = tour_airports(t)
        if not A or not D:
            continue
        for row in t.get('departures') or []:
            try:
                s, e = dt.date.fromisoformat(row['start']), dt.date.fromisoformat(row['end'])
            except Exception:
                continue
            if s <= today or (row.get('seats') == 0):
                continue
            for o in ORIGINS:
                need.setdefault((o, A), set()).update({s, s - dt.timedelta(days=1)})
                need.setdefault((D, o), set()).add(e)
    cache, prices = {}, {}

    def one(item):
        (o, d), dates = item
        out = {}
        for month in sorted({x.strftime('%Y-%m') for x in dates}):
            best = month_prices(token, o, d, month, cache)
            for x in dates:
                if x.strftime('%Y-%m') == month and x.isoformat() in best:
                    out[x.isoformat()] = best[x.isoformat()]
        return f'{o}-{d}', out

    with ThreadPoolExecutor(max_workers=3) as ex:   # 3 потока: ~45 городов за несколько минут, без перегрузки API
        for k, out in ex.map(one, sorted(need.items())):
            if out:
                prices[k] = out
    calls = len(cache)
    OUT.write_text(json.dumps({'updated': dt.datetime.now(dt.timezone.utc).strftime('%Y-%m-%d %H:%M UTC'),
                               'origins': ORIGINS, 'prices': prices}, ensure_ascii=False, separators=(',', ':')), encoding='utf-8')
    print('направлений:', len(need), 'запросов:', calls, 'с ценами:', len(prices), 'дат с ценой:', sum(len(v) for v in prices.values()))


if __name__ == '__main__':
    main()
