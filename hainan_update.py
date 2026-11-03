#!/usr/bin/env python3
"""Хайнань для «Отелей TEZ»: раз в неделю собирает hainan.json.

1) Факты по отелям нашей подборки — со страницы отеля на tez-tour.com
   (расположение, пляж, год постройки/реновации, «Внимание»: мин. срок, депозит, обязательные ужины).
   Список отелей берётся из BAYS в hotels.html (id TEZ).
2) Перелёты на Хайнань — из открытого поиска TEZ: из каких городов России есть туры,
   в какой аэропорт (Санья / Хайкоу, регулярный или чартер), цена перелёта на человека туда-обратно,
   в какие месяцы есть вылеты (flightcalendar).

Запуск: python3 podbor/hainan_update.py  (GitHub Actions — .github/workflows/hainan.yml, по понедельникам)
Только стандартная библиотека. Ограничение TEZ — 50 запросов в минуту, поэтому паузы между запросами.
"""
import datetime as dt
import html
import json
import re
import subprocess
import time
import urllib.parse
from pathlib import Path

HERE = Path(__file__).resolve().parent
OUT = HERE / 'hainan.json'
API = 'https://search.tez-tour.com/tariffsearch/'
CHINA, HAINAN_TOUR, USD = 140009, 335561, 5561
UA = {'User-Agent': 'Mozilla/5.0 (planmydream-podbor; weekly hainan update)'}
PAUSE = 1.6   # ≤ 40 запросов в минуту

MONTHS = ['янв', 'фев', 'мар', 'апр', 'май', 'июн', 'июл', 'авг', 'сен', 'окт', 'ноя', 'дек']


def get(url, tries=3):
    # curl, а не urllib: сайт TEZ отдаёт неполную цепочку сертификатов — Python её не принимает, curl принимает
    for i in range(tries):
        try:
            r = subprocess.run(['curl', '-sS', '-L', '--max-time', '90', '-A', UA['User-Agent'], url],
                               capture_output=True, timeout=120)
            if r.returncode == 0 and r.stdout:
                return r.stdout.decode('utf-8', 'ignore')
            err = r.stderr.decode('utf-8', 'ignore').strip() or f'curl код {r.returncode}'
        except Exception as e:
            err = str(e)
        finally:
            time.sleep(PAUSE)
        if i == tries - 1:
            raise RuntimeError(err)
        time.sleep(10 * (i + 1))


def api(path, **params):
    q = urllib.parse.urlencode(params, doseq=True)
    return json.loads(get(API + path + '?' + q))


def clean(s):
    s = re.sub(r'<br\s*/?>', '\n', s, flags=re.I)
    s = html.unescape(re.sub(r'<[^>]+>', '', s))
    s = re.sub(r'[ \t\r\f\v]+', ' ', s)
    return '\n'.join(x.strip() for x in s.split('\n') if x.strip())


# ---------- отели ----------
def hotel_ids():
    src = (HERE / 'hotels.html').read_text(encoding='utf-8')
    block = src[src.index('const BAYS'):src.index('const AIRPORT')]
    return sorted({int(x) for x in re.findall(r"\['[^']*',\s*(\d+),", block)})


def hotel_facts(hid):
    url = f'https://www.tez-tour.com/hotel.html?id={hid}'
    page = get(url)
    facts = {}
    # разделы вида <p><b>Расположение:</b><br> текст</p>
    for label, body in re.findall(r'<p>\s*<b>\s*([^<:]{2,40}):\s*</b>(.*?)</p>', page, flags=re.S):
        label, text = clean(label), clean(body)
        if text and label not in facts:
            facts[label] = text[:1200]
    # «Внимание» — условия: мин. срок, депозит, обязательные ужины, дети
    notes = []
    m = re.search(r'id="t010"[^>]*>(.*?)</div>', page, flags=re.S)
    if m:
        for p in re.findall(r'<p>(.*?)</p>', m.group(1), flags=re.S):
            t = clean(p)
            if t and not t.startswith('Ex.'):
                notes.append(t[:600])
    # короткая характеристика под рейтингом
    m = re.search(r'Рейтинг отеля:\s*</[^>]+>\s*(?:<[^>]+>\s*)*([^<]{10,200})<', page)
    blurb = clean(m.group(1)) if m else ''
    return {'url': url, 'facts': facts, 'notes': notes, 'blurb': blurb}


# ---------- перелёты ----------
def flights():
    refs = api('refs', locale='ru')
    cities = [c for c in refs['cities'] if c['countryId'] == 1102]
    today = dt.date.today()
    windows = [today + dt.timedelta(days=d) for d in (30, 75, 150)]
    out = []
    for c in sorted(cities, key=lambda x: x['name']):
        try:
            if CHINA not in api('permitcountries', cityId=c['cityId']).get('permitCountries', []):
                continue
            arrivals, prices, months = {}, [], set()
            for w in windows:
                j = api('getResult', locale='ru', cityId=c['cityId'], countryId=CHINA, tourId=HAINAN_TOUR,
                        after=w.strftime('%d.%m.%Y'), before=(w + dt.timedelta(days=10)).strftime('%d.%m.%Y'),
                        nightsMin=7, nightsMax=7, accommodationId=2, currency=USD, priceMin=0, priceMax=149999,
                        hotelClassId=2568, hotelClassBetter='true', rAndBId=15350, rAndBBetter='true',
                        tourType=1, groupByHotel=2, promoFlag='true')
                for r in j.get('data') or []:
                    reg = r[5]
                    name = reg[4] or reg[0]
                    arrivals[name] = arrivals.get(name, 0) + 1
                    try:
                        fl = r[13][0]
                        prices.append(float(fl['to']['price']) + float(fl['from']['price']))
                    except Exception:
                        pass
            if not arrivals:
                continue
            cal = api('flightcalendar', cityId=c['cityId'], countryId=CHINA, locale='ru').get('data') or []
            for year_row in cal:
                year = year_row[0]
                for mi, days in enumerate(year_row[1:13]):
                    d = dt.date(year, mi + 1, 1)
                    if days and d >= today.replace(day=1):
                        months.add((year, mi + 1))
            out.append({
                'city': c['name'], 'cityId': c['cityId'],
                'arrivals': sorted(arrivals, key=lambda k: -arrivals[k]),
                'flightFrom': round(min(prices)) if prices else None,
                'months': [f'{MONTHS[m - 1]} {str(y)[2:]}' for y, m in sorted(months)][:14],
            })
        except Exception as e:
            print('город', c['name'], 'ошибка', e)
    return out


def main():
    old = json.loads(OUT.read_text(encoding='utf-8')) if OUT.exists() else {}
    data = {'updated': dt.datetime.now(dt.timezone.utc).strftime('%Y-%m-%d %H:%M UTC'), 'hotels': {}, 'flights': []}
    for hid in hotel_ids():
        try:
            data['hotels'][str(hid)] = hotel_facts(hid)
        except Exception as e:
            print('отель', hid, 'ошибка', e)
            if str(hid) in old.get('hotels', {}):
                data['hotels'][str(hid)] = old['hotels'][str(hid)]
    try:
        data['flights'] = flights()
    except Exception as e:
        print('перелёты: ошибка', e)
    if not data['flights'] and old.get('flights'):
        data['flights'], data['flightsStale'] = old['flights'], True
    OUT.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding='utf-8')
    print('отелей:', len(data['hotels']), 'городов с перелётом:', len(data['flights']))


if __name__ == '__main__':
    main()
