"""Автодописывание подборщика через Claude: «Что вас ждёт» по городам, схема переездов, темп и формат.

Когда зовём Claude:
  * тура нет в ручных данных extra.js (новый тур на сайте);
  * программа тура на сайте поменялась с тех пор, как записаны ручные или прошлые автоданные.
Результат хранится в auto.json (ключ — адрес тура) и попадает в data.json как tour['auto'];
подборщик показывает его с пометкой «составлено автоматически — проверьте».
Без ключа ANTHROPIC_API_KEY ничего не генерируется — только предупреждение.
"""
import hashlib, json, os, re
from pathlib import Path

AUTO = Path(__file__).with_name('auto.json')
EXTRA = Path(__file__).with_name('extra.js')
MODEL = 'claude-opus-5-5'

SCHEMA = {
    'type': 'object',
    'properties': {
        'see': {'type': 'array', 'items': {
            'type': 'object',
            'properties': {'city': {'type': 'string'}, 'what': {'type': 'string'}, 'benefit': {'type': 'string'}},
            'required': ['city', 'what', 'benefit'], 'additionalProperties': False}},
        'legs': {'type': 'array', 'items': {
            'type': 'object',
            'properties': {'day': {'type': 'integer'}, 'from': {'type': 'string'}, 'to': {'type': 'string'},
                           'transport': {'type': 'string', 'enum': ['plane', 'train', 'bus', 'night', 'move']},
                           'time': {'type': 'string'}},
            'required': ['day', 'from', 'to', 'transport', 'time'], 'additionalProperties': False}},
        'arrive': {'type': 'string'},
        'depart': {'type': 'string'},
        'format': {'type': 'array', 'items': {'type': 'string', 'enum': ['classic', 'nature', 'city', 'practice', 'holiday', 'beach']}},
        'pace': {'type': 'string', 'enum': ['calm', 'medium', 'active']},
        'adultsOnly': {'type': 'boolean'},
        'note': {'type': 'string'},
    },
    'required': ['see', 'legs', 'arrive', 'depart', 'format', 'pace', 'adultsOnly', 'note'],
    'additionalProperties': False,
}

SYSTEM = """Ты помогаешь менеджеру турагентства «План Мечты» презентовать авторский тур по Китаю на звонке.
По данным тура с сайта составь короткие подсказки на русском языке. Пиши только то, что есть в данных тура; ничего не выдумывай.

see — «Что вас ждёт»: по одной строке на КАЖДЫЙ город или локацию маршрута, в порядке маршрута.
  city — название; what — главные места и впечатления через запятую (коротко, 5–12 слов);
  benefit — чем это ценно туристу, 3–8 слов, без восклицаний.
legs — все переезды внутри Китая между городами, по дням: и перелёты, и поезда, и автобусы, и поездки на день туда-обратно.
  Смотри и программу по дням, и «Что включено» (там часто перечислены перелёты и поезда).
  transport: plane | train | bus | night (ночной поезд) | move (транспорт не указан).
  time — только если время или длительность есть в данных («~3 ч», «09:17–11:20»); иначе пустая строка.
  Для поездок на день допиши в time «на день, вечером обратно».
arrive / depart — город прилёта и вылета домой.
format — подходящие из: classic (история, классика), nature (природа, горы), city (мегаполисы), practice (практики, ретрит), holiday (праздник), beach (пляж).
pace — calm (спокойный), medium (средний), active (много переездов, долгие пешие маршруты, высокогорье).
adultsOnly — true, только если в данных прямо сказано, что с детьми нельзя.
note — одна короткая важная для туриста оговорка (высокогорье, ночной поезд, опция) или пустая строка.
Если сайт и расписание договора расходятся, в note так и напиши: «сайт и договор расходятся: …»."""


def _norm(text):
    # даты меняются от заезда к заезду — для сравнения программ они не важны
    return re.sub(r'\s+', ' ', re.sub(r'\([^)]*\d[^)]*\)', '', text or '')).strip()


def program_hash(t):
    contract = t['contract'][0]['text'] if t.get('contract') else ''
    key = json.dumps([t['route'], [_norm(d) for d in t['days']], _norm(contract), _norm(t['included'])], ensure_ascii=False)
    return hashlib.sha1(key.encode('utf-8')).hexdigest()[:16]


def manual_slugs():
    """Адреса туров, для которых в extra.js есть ручные «Что вас ждёт» (TOUR_SEE)."""
    js = EXTRA.read_text(encoding='utf-8')
    block = js[js.find('window.TOUR_SEE'):]
    return set(re.findall(r"^\s+'([\w\-]+)': \[", block, re.M))


def _prompt(t):
    contract = t['contract'][0]['text'] if t.get('contract') else '—'
    return (f"Тур: {t['title']}\nОрганизатор: {t['org']}\nМаршрут (каталог): {t['route']}\n"
            f"Места (фильтр каталога): {', '.join(t['sights'])}\n\n"
            f"Что включено:\n{t['included']}\n\nОписание первого экрана:\n{t['intro']}\n\n"
            f"Программа по дням (сайт):\n" + '\n\n'.join(t['days']) +
            f"\n\nРасписание как в договоре:\n{contract}")


def _generate(client, t):
    response = client.beta.messages.create(
        model=MODEL,
        max_tokens=16000,
        betas=['server-side-fallback-2026-07-01'],
        fallbacks='default',
        system=SYSTEM,
        output_config={'effort': 'medium', 'format': {'type': 'json_schema', 'schema': SCHEMA}},
        messages=[{'role': 'user', 'content': _prompt(t)}],
    )
    if response.stop_reason == 'refusal':
        raise RuntimeError('модель отказалась отвечать')
    text = next(b.text for b in response.content if b.type == 'text')
    return json.loads(text)


def autofill(tours, warnings):
    """Дописывает tour['auto'] там, где ручных данных нет или программа изменилась."""
    store = json.loads(AUTO.read_text(encoding='utf-8')) if AUTO.exists() else {}
    manual = manual_slugs()
    client = None
    for t in tours:
        if not t['departures'] and t['slug'] not in store:
            continue  # прошедший тур без дат — не тратим запрос
        h = program_hash(t)
        rec = store.get(t['slug'], {})
        if t['slug'] in manual and 'manualHash' not in rec:
            rec['manualHash'] = h  # первая встреча: ручные данные считаем актуальными для этой программы
        fresh_manual = t['slug'] in manual and rec.get('manualHash') == h
        if fresh_manual:
            rec.pop('data', None)
        elif rec.get('hash') != h:
            key = os.environ.get('ANTHROPIC_API_KEY')
            if not key:
                warnings.append(f"{t['slug']}: новый тур или изменилась программа — нет ключа ANTHROPIC_API_KEY, автодописывание пропущено")
            else:
                try:
                    if client is None:
                        import anthropic
                        client = anthropic.Anthropic()
                    rec['data'] = _generate(client, t)
                    rec['hash'] = h
                    rec['at'] = t.get('_now', '')
                    print(f"  ИИ дописал: {t['slug']}")
                except Exception as e:  # сбой одного тура не должен ронять сборку
                    warnings.append(f"{t['slug']}: автодописывание не удалось ({e})")
        store[t['slug']] = rec
        if rec.get('data') and not fresh_manual:
            t['auto'] = {**rec['data'], 'at': rec.get('at', ''), 'reason': 'новый тур' if t['slug'] not in manual else 'программа на сайте изменилась'}
    AUTO.write_text(json.dumps(store, ensure_ascii=False, indent=1), encoding='utf-8')
