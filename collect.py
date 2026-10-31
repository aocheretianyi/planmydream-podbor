#!/usr/bin/env python3
"""Сборщик данных для подборщика туров «План Мечты».

Первоисточник — сайт planmydream.ru:
  * каталог туров Tilda Store — название, организатор, цена, маршрут, места/фильтры, заезды (даты, места);
  * страница каждого тура — программа по дням, «Что включено / не включено», порядок оплаты,
    доп. услуги, FAQ, проживание, описание первого экрана, ссылка на буклет.
Из Google-таблицы берутся только продающий скрипт («свойство → выгода») и ссылка на буклет: на сайте их нет.

Запуск:  python3 podbor/collect.py      → пишет podbor/data.json
Страницы качаются через curl (на Mac у python.org-Python нет сертификатов, а curl есть везде).
"""
import csv, html, io, json, re, subprocess, sys, time
from datetime import date, datetime, timezone
from html.parser import HTMLParser
from pathlib import Path

STORE = ('https://store.tildaapi.one/api/getproductslist/?storepartuid=853517125224&recid=4248233303'
         '&getparts=true&getoptions=true&slice=1&size=200&flag_root=withroot')
SHEET = ('https://docs.google.com/spreadsheets/d/1my6kd5FyYn_jJghSNghPToV2esLx8eFlUUokhLwtFNI/gviz/tq'
         '?tqx=out:csv&sheet=%D0%A2%D1%83%D1%80%D1%8B')
OUT = Path(__file__).with_name('data.json')
MONTHS = ['января', 'февраля', 'марта', 'апреля', 'мая', 'июня', 'июля', 'августа',
          'сентября', 'октября', 'ноября', 'декабря']


def fetch(url, referer=None, tries=4):
    cmd = ['curl', '-sL', '--max-time', '40', '-A', 'Mozilla/5.0 (podbor)', url]
    if referer:
        cmd[1:1] = ['-H', f'Referer: {referer}']
    for i in range(tries):
        out = subprocess.run(cmd, capture_output=True).stdout.decode('utf-8', 'replace')
        if len(out) > 500:  # защита сайта (ddos-guard) иногда отдаёт пустой ответ
            return out
        time.sleep(2 + 3 * i)
    raise RuntimeError(f'не удалось скачать {url}')


class Fields(HTMLParser):
    """Собирает текст всех элементов с атрибутом field (так Tilda помечает редактируемые поля),
    запоминая блок (rec) и его тип."""
    BLOCK = {'br', 'p', 'li', 'div', 'ul', 'ol', 'h1', 'h2', 'h3', 'h4'}

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.rec = (None, None)
        self.stack = []      # открытые поля: [имя поля, глубина, текстовые куски]
        self.depth = 0
        self.items = []      # (rec_id, rec_type, field, text)
        self.links = []

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        if tag == 'a' and a.get('href'):
            self.links.append(a['href'])
        if tag == 'div' and (a.get('id') or '').startswith('rec') and a.get('data-record-type'):
            self.rec = (a['id'][3:], a['data-record-type'])
        if tag in ('br',):
            self._text('\n')
            return
        if tag in self.BLOCK or tag == 'li':
            self._text('\n' + ('• ' if tag == 'li' else ''))
        if tag in ('img', 'input', 'meta', 'link', 'hr', 'source'):
            return
        self.depth += 1
        if a.get('field'):
            self.stack.append([a['field'], self.depth, []])

    def handle_endtag(self, tag):
        if tag in ('br', 'img', 'input', 'meta', 'link', 'hr', 'source'):
            return
        if tag in self.BLOCK:
            self._text('\n')
        if self.stack and self.stack[-1][1] == self.depth:
            name, _, parts = self.stack.pop()
            text = clean(''.join(parts))
            if text:
                self.items.append((self.rec[0], self.rec[1], name, text))
            if self.stack:  # вложенное поле — текст нужен и родителю
                self.stack[-1][2].append(text)
        self.depth -= 1

    def handle_data(self, data):
        self._text(data)

    def _text(self, t):
        if self.stack:
            self.stack[-1][2].append(t)


def clean(t):
    t = t.replace('\xa0', ' ')
    t = re.sub(r'[ \t]+', ' ', t)
    t = re.sub(r' *\n *', '\n', t)
    t = re.sub(r'\n{3,}', '\n\n', t)
    return t.strip()


def parse_page(page):
    p = Fields()
    p.feed(page)
    titles, descrs = {}, {}
    days, hotels, intro = {}, '', ''
    for rec, typ, field, text in p.items:
        if typ == '585' and field.startswith('li_title__'):
            titles[(rec, field[10:])] = text
        elif typ == '585' and field.startswith('li_descr__'):
            descrs[(rec, field[10:])] = text
        elif typ == '762' and field in ('title', 'subtitle', 'descr'):
            days.setdefault(rec, []).append(text)  # в одних турах «День N» в заголовке, в других — в тексте
        elif field == 'btitle' and 'Где будем жить' in text:
            hotels = next((t for r, _, f, t in p.items if r == rec and f == 'bdescr'), '')
        elif not intro and 'Длительность' in text and 'Маршрут' in text and len(text) > 150:
            intro = text
    days = ['\n'.join(v) for v in days.values()]
    acc = [(titles[k], descrs.get(k, '')) for k in titles]
    sect = {t: d for t, d in acc}
    service = {'Что включено', 'Что не включено', 'Порядок оплаты', 'Дополнительные услуги'}
    faq = [{'q': t, 'a': d} for t, d in acc if t not in service and d]
    booklet = next((l for l in p.links if 'bookflip' in l), '')
    return {
        'included': sect.get('Что включено', ''), 'excluded': sect.get('Что не включено', ''),
        'payment': sect.get('Порядок оплаты', ''), 'extras': sect.get('Дополнительные услуги', ''),
        'faq': faq, 'days': days, 'hotels': hotels, 'intro': intro, 'booklet': booklet,
    }


def ru_range(a, b):
    if a.month == b.month:
        return f'{a.day}–{b.day} {MONTHS[b.month - 1]} {b.year}'
    if a.year == b.year:
        return f'{a.day} {MONTHS[a.month - 1]} – {b.day} {MONTHS[b.month - 1]} {b.year}'
    return f'{a.day} {MONTHS[a.month - 1]} {a.year} – {b.day} {MONTHS[b.month - 1]} {b.year}'


def message(t, dep):
    """Сообщение клиенту — в том же формате, что раньше лежал в таблице, но из данных сайта."""
    a, b = date.fromisoformat(dep['start']), date.fromisoformat(dep['end'])
    n = (b - a).days + 1
    incl = re.search(r'(Вс[её] включено[^\n]*?)(?:\n|Длительность|$)', t['intro'])
    lines = [f'"{t["title"].upper()}"',
             f'📅 Даты: {ru_range(a, b)}',
             f'🗺 Маршрут: {t["route"]}',
             f'⏱️ Длительность: {n} дней ({n - 1} ночей)',
             f'💰 Стоимость: ${t["price"]}']
    if incl:
        lines.append('✅ ' + incl.group(1).strip().rstrip('.'))
    lines.append(f'🔗 Ссылка: {t["url"]}')
    if t['booklet']:
        lines.append(f'📖 Буклет: {t["booklet"]}')
    return '\n'.join(lines)


def scripts_from_sheet():
    """Продающие скрипты из таблицы: ключ — адрес тура из колонки «Сообщение для клиента»."""
    try:
        rows = list(csv.reader(io.StringIO(fetch(SHEET))))
    except Exception as e:  # таблица — не главное, без неё подборщик работает
        print('! таблица не загрузилась:', e, file=sys.stderr)
        return {}
    h = rows[0]
    ci = next(i for i, x in enumerate(h) if x.startswith('Продающий скрипт'))
    cm = next(i for i, x in enumerate(h) if x.startswith('Сообщение для клиента'))
    out = {}
    for r in rows[1:]:
        m = re.search(r'planmydream\.ru/([\w\-]+)', r[cm] if len(r) > cm else '')
        if m and r[ci].strip() and m.group(1) not in out:
            b = re.search(r'https://\S*bookflip\S*', r[cm])
            out[m.group(1)] = {'script': r[ci].strip(), 'booklet': b.group(0) if b else ''}
    return out


def num(v):
    try:
        return int(float(v))
    except (TypeError, ValueError):
        return None


def main():
    store = json.loads(fetch(STORE, referer='https://planmydream.ru/'))
    scripts = scripts_from_sheet()
    today = date.today().isoformat()
    tours, warnings = [], []
    for pr in store['products']:
        ch = {}
        for c in pr.get('characteristics', []):
            ch.setdefault(c['title'], []).append(c['value'].strip())
        url = pr['url'].rstrip('/')
        slug = url.split('/')[-1]
        deps = []
        for z in ch.get('Заезд', []):
            s, e, price, seats, *_ = (z.split('|') + ['', '', '', ''])[:5]
            if s >= today:
                deps.append({'start': s, 'end': e, 'priceFull': num(price), 'seats': num(seats)})
        deps.sort(key=lambda d: d['start'])
        t = {
            'slug': slug, 'url': url, 'title': pr['title'].strip(),
            'org': (ch.get('Организатор') or [''])[0],
            'price': num(pr.get('price')) or (deps[0]['priceFull'] if deps else None),
            'priceOld': num(pr.get('priceold')),
            'route': (ch.get('Маршрут') or [''])[0],
            'sights': ch.get('Достопримечательность', []),
            'duration': (ch.get('Продолжительность') or [''])[0],
            'image': (json.loads(pr['gallery'])[0]['img'] if pr.get('gallery') else ''),
            'departures': deps, 'script': scripts.get(slug, {}).get('script', ''),
        }
        try:
            t.update(parse_page(fetch(url)))
        except Exception as e:
            warnings.append(f'{slug}: страница не загрузилась ({e})')
            t.update(parse_page(''))
        if not t['booklet']:  # ссылки на флипбук на страницах нет — берём из таблицы
            t['booklet'] = scripts.get(slug, {}).get('booklet', '')
        for d in deps:
            d['message'] = message(t, d)
        for need, label in [('included', 'Что включено'), ('payment', 'Порядок оплаты'), ('days', 'программа по дням')]:
            if not t[need]:
                warnings.append(f'{slug}: на странице не найдено «{label}»')
        if not t['script']:
            warnings.append(f'{slug}: нет продающего скрипта в таблице')
        tours.append(t)
        time.sleep(0.7)
    data = {'generatedAt': datetime.now(timezone.utc).isoformat(timespec='seconds'),
            'source': 'planmydream.ru (каталог + страницы туров)', 'tours': tours, 'warnings': warnings}
    OUT.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding='utf-8')
    print(f'Готово: {len(tours)} туров → {OUT}')
    for w in warnings:
        print('!', w)


if __name__ == '__main__':
    main()
