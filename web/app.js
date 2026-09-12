/* MOEX Radar — сборка страницы из data.json (docs/CONTRACT.md §3).
 *
 * Ничего не выдумываем: на экран попадает только то, что положил конвейер. Пустое
 * поле рисуется честным «нет данных», а не прочерком, за которым не видно разницы
 * между «источник молчит» и «значение равно нулю».
 *
 * Цвет нигде не работает в одиночку: у состояния ячейки, статуса источника и
 * вердикта сигнала рядом с цветом всегда стоят иконка и словесная подпись — это
 * требование доступности и заодно защита от чтения панели в оттенках серого.
 *
 * Первая строка панели — ПОЗИЦИЯ (акции / деньги), итог ворот и наклона; её
 * считает конвейер (compute/decision.py), фронт не выводит решение сам. Поля
 * аудита 02.09.2026 (verdict.position, states.gate, states.regime_stats, shadow,
 * тайл expectations) необязательны: витрина без них рисуется как прежде, новые
 * блоки просто не появляются — старый data.json остаётся законным входом.
 */
(function () {
  'use strict';

  var C = window.Charts;
  var h = C.h, isNum = C.isNum, fmtNum = C.fmtNum, fmtDay = C.fmtDay, fmtMon = C.fmtMon, tok = C.tok;

  var DATA_URL = '/data/data.json';
  var REFRESH_MS = 60000;

  // Хвосты, которые конвейер добавляет к заметке тайла по тиру. На экране их несёт
  // бейдж (в подсказке), поэтому из текста заметки срезаем: пятнадцать одинаковых
  // абзацев про «предиктивность не доказана» — это шум, в котором тонет то самое
  // предложение, ради которого заметка и написана.
  var TIER_TAILS = [
    'Мониторинг: предиктивность не доказана (мало истории или событий)',
    'Направление подтверждено, сила умеренная/режимная',
    'Валидировано: значимо на истории и переживает поправки',
    'Как предиктор акций опровергнуто — контекст, не сигнал'
  ];
  function trimNote(note) {
    if (!note) return '';
    var out = String(note);
    TIER_TAILS.forEach(function (tail) {
      var i = out.indexOf(tail);
      if (i >= 0) out = out.slice(0, i);
    });
    return out.replace(/[\s.;·]+$/, '').trim();
  }

  /* Числа, пришедшие текстом из конвейера, набраны «по-английски»: десятичная
   * точка и минус-дефис. Всё, что печатает фронт, идёт через toLocaleString('ru-RU')
   * — запятая и типографский минус. В одной строке («z +0,87 · +11.3% за 63д») это
   * читается как брак, поэтому приводим прозу конвейера к виду панели в одном месте.
   *
   * Грабли: даты — тоже точки. «до заседания 11.09», «аукцион 05.08», окно
   * «10.09–05.10» превратились бы в «11,09». Поэтому дд.мм[.гггг] с осмысленным
   * номером месяца из замены выводим. Обратная сторона эвристики: десятичное
   * число вида 26.11 конвейер напечатает как дату — но такие значения у нас
   * идут с одним знаком после точки либо с невозможным «месяцем» (14.00%).
   */
  function ruText(s) {
    if (s == null) return '';
    return String(s)
      .replace(/(^|[\s(«])-(?=\d)/g, '$1−')
      .replace(/\d+(?:\.\d+)+/g, function (t) {
        if (/^\d{2}\.\d{2}(\.\d{4})?$/.test(t)) {
          var dd = +t.slice(0, 2), mm = +t.slice(3, 5);
          if (dd >= 1 && dd <= 31 && mm >= 1 && mm <= 12) return t;
        }
        return t.split('.').join(',');
      });
  }

  // «2026-08-04» -> «04.08» — так даты пишет вся панель. Год опускается: подпись
  // сигнала живёт в строке рядом с z и значением, и полный год съедал бы её ширину;
  // отставание больше года невозможно — ffill в панели ограничен днями.
  function ruDay(iso) {
    var s = String(iso || '');
    return /^\d{4}-\d{2}-\d{2}/.test(s) ? s.slice(8, 10) + '.' + s.slice(5, 7) : s;
  }

  // Перцентили конвейер публикует уже в процентах: monitors._pct_last возвращает
  // 0..100, и headline тайла говорит «78-й перцентиль» из того же числа. Умножение
  // на сто давало на экране «7800-й перцентиль». Доли (hit, weight) — другая
  // величина, они приходят 0..1 и множатся на сто законно.
  function pctile(v) { return isNum(v) ? Math.round(v) : null; }

  var TIER_NOTE = {
    A: 'Тир A — валидировано: значимо на истории и переживает поправки на множественность.',
    B: 'Тир B — направление подтверждено, сила умеренная или режимная.',
    monitor: 'Мониторинг — предиктивность не доказана (мало истории или событий). Наблюдаем, не торгуем.',
    dead: 'Как предиктор рынка акций опровергнуто. Оставлено для контекста.'
  };

  /* ────────────────────────────────────────────────────────── иконки */

  function ico(name, cls) {
    var paths = {
      good: 'M2.5 8.4l3.3 3.3 7.7-7.7',
      warn: 'M8 1.8l6.4 11.4H1.6zM8 6.2v3.4M8 11.3v.1',
      crit: 'M4 4l8 8M12 4l-8 8',
      flat: 'M3 8h10',
      up: 'M8 12.5V3.5M4 7l4-3.5L12 7',
      down: 'M8 3.5v9M4 9l4 3.5L12 9',
      info: 'M8 7.2v4.4M8 4.6v.1',
      clock: 'M8 4.2V8l2.6 1.6',
      lock: 'M4.6 7V5.4a3.4 3.4 0 016.8 0V7M3.6 7h8.8v6H3.6z'
    };
    var svg = document.createElementNS('http://www.w3.org/2000/svg', 'svg');
    svg.setAttribute('viewBox', '0 0 16 16');
    svg.setAttribute('fill', 'none');
    svg.setAttribute('stroke', 'currentColor');
    svg.setAttribute('stroke-width', '1.8');
    svg.setAttribute('stroke-linecap', 'round');
    svg.setAttribute('stroke-linejoin', 'round');
    svg.setAttribute('aria-hidden', 'true');
    if (cls) svg.setAttribute('class', cls);
    if (name === 'info' || name === 'clock') {
      var circ = document.createElementNS('http://www.w3.org/2000/svg', 'circle');
      circ.setAttribute('cx', '8'); circ.setAttribute('cy', '8'); circ.setAttribute('r', '6.2');
      svg.appendChild(circ);
    }
    var p = document.createElementNS('http://www.w3.org/2000/svg', 'path');
    p.setAttribute('d', paths[name] || paths.info);
    svg.appendChild(p);
    return svg;
  }

  /* ──────────────────────────────────────────────────────────── тема */

  var THEMES = ['auto', 'light', 'dark'];
  var THEME_LABEL = { auto: 'Как в системе', light: 'Светлая', dark: 'Тёмная' };

  // Тема, которую панель показывает прямо сейчас. Держим её здесь, а не вычитываем
  // каждый раз заново: ?theme=… в адресе не меняется от нажатий, поэтому цикл,
  // считавший следующий шаг от currentTheme(), после первого нажатия навсегда
  // возвращался в ту же точку — кнопка на такой ссылке была мертва.
  var themeState = 'auto';

  function currentTheme() {
    // Разбор темы живёт в раннем скрипте index.html — он ставит её до первой
    // отрисовки, и вторая копия правила однажды разошлась бы с первой. Порядок там
    // такой: тема из адреса, затем вечернее правило, затем сохранённый выбор.
    return window.__theme.resolve();
  }
  function applyTheme(t, persist) {
    themeState = t;
    document.documentElement.dataset.theme = (t === 'auto' ? '' : t);
    // В хранилище пишем только собственный выбор посетителя. Тема из адреса —
    // свойство ссылки, а не человека: раньше она затирала сохранённый выбор
    // навсегда, и получатель ссылки терял свою тему на всех вкладках.
    // Вечером выбор живёт до закрытия вкладки и не трогает постоянную память: иначе
    // один вечер сделал бы тёмную «последней темой» навсегда.
    if (persist) window.__theme.remember(t);
    var label = document.getElementById('theme-label');
    if (label) label.textContent = THEME_LABEL[t];
    var btn = document.getElementById('theme-toggle');
    if (btn) btn.setAttribute('title', 'Тема: ' + THEME_LABEL[t] + ' — нажмите, чтобы сменить');
    // Графики читают цвета из CSS-переменных в момент отрисовки, поэтому при смене
    // темы их нужно перерисовать — иначе линии останутся в палитре прежней темы.
    if (window.__lastPayload) render(window.__lastPayload, true);
  }
  function initTheme() {
    applyTheme(currentTheme(), false);
    var btn = document.getElementById('theme-toggle');
    if (!btn) return;
    btn.addEventListener('click', function () {
      applyTheme(THEMES[(THEMES.indexOf(themeState) + 1) % THEMES.length], true);
    });
  }

  /* ─────────────────────────────────────────────────── общие кусочки */

  function tier(t) {
    if (!t) return null;
    var map = { A: 'A', B: 'B', monitor: 'монитор', dead: 'опровергнуто' };
    return h('span', {
      'class': 'tier tier--' + t, title: TIER_NOTE[t] || '',
      text: map[t] || t
    });
  }

  function statusDot(status) {
    var label = { ok: 'свежие данные', stale: 'данные устарели', error: 'источник не ответил',
                  missing: 'данных ещё нет',
                  manual_needed: 'живой источник молчит — показан ручной резерв' };
    // Неизвестный статус красим как stale, а не как «свежие»: молчаливое
    // превращение чужого слова в зелёную точку — это ровно то, как ручной резерв
    // дивидендов месяц выдавался за живые данные (аудит 20.08.2026).
    var known = { ok: 1, stale: 1, error: 1, missing: 1 };
    return h('span', {
      'class': 'dot-status dot-status--' + (known[status] ? status : (status ? 'stale' : 'missing')),
      title: label[status] || status, role: 'img', 'aria-label': label[status] || status
    });
  }

  function section(title, sub, kids) {
    return h('section', { 'class': 'section' }, [
      h('div', { 'class': 'section__head' }, [
        h('h2', { 'class': 'section__title', text: title }),
        sub ? h('span', { 'class': 'section__sub', text: sub }) : null
      ])
    ].concat(kids));
  }

  function stat(k, v, hint, cls) {
    return h('div', null, [
      h('div', { 'class': 'stat__k', text: k }),
      h('div', { 'class': 'stat__v' + (cls ? ' ' + cls : ''), text: v }),
      hint ? h('div', { 'class': 'stat__hint', text: hint }) : null
    ]);
  }

  function toneOf(v) { return !isNum(v) ? 'tone-mut' : (v > 0 ? 'tone-pos' : (v < 0 ? 'tone-neg' : 'tone-mut')); }

  /* ─────────────────────────────────────────────────────── вердикт */

  var BIT_LABEL = {
    trend: { k: 'Тренд', on: 'бык', off: 'медведь' },
    vol: { k: 'Волатильность', on: 'стресс', off: 'спокойно' },
    bond: { k: 'Облигации', on: 'стресс', off: 'спокойно' }
  };
  var PHASE = { '-1': 'смягчение', '0': 'пауза', '1': 'ужесточение' };

  // Порядок в строке котировок: первым — сам индекс, панель о нём; дальше то,
  // чем его движение объясняют. Знаков после запятой ровно столько, сколько
  // несёт смысл в конкретном инструменте.
  var QUOTE_ORDER = ['imoex', 'rgbi', 'rvi', 'cny_tom', 'brent_moex', 'gld_tom'];
  var QUOTE_DIGITS = { imoex: 2, rgbi: 2, rvi: 1, cny_tom: 3, brent_moex: 2, gld_tom: 0 };

  function ageWord(minutes) {
    if (!isNum(minutes)) return null;
    if (minutes < 1) return 'только что';
    if (minutes < 60) return Math.round(minutes) + ' мин назад';
    if (minutes < 1440) return Math.floor(minutes / 60) + ' ч назад';
    return Math.round(minutes / 1440) + ' дн. назад';
  }

  /* Строка котировок в шапке героя.
   *
   * Конвейер считает блок quotes каждые пять минут именно ради витрины, но панель
   * по индексу МосБиржи не показывала сам индекс: первый вопрос читателя («где
   * рынок сейчас») оставался без ответа, а интрадей-прогон — без единого видимого
   * следа на экране. Возраст берём худший из показанных цен: строка честна ровно
   * настолько, насколько устарел самый несвежий её элемент.
   *
   * Грабли: age_min конвейер считает на момент ПУБЛИКАЦИИ, а не на момент чтения.
   * Если витрина зависла на три часа, цены зависли вместе с ней — поэтому к
   * возрасту цены всегда прибавляем возраст самой витрины.
   */
  function renderQuotes(d) {
    var q = d.quotes || {};
    var keys = QUOTE_ORDER.filter(function (k) { return q[k] && isNum(q[k].value); });
    if (!keys.length) return null;
    var pub = ageMinutes(d.generated_at);
    var age = null;
    var pills = keys.map(function (k) {
      var x = q[k];
      // К возрасту цены прибавляем задержку САМОГО ИСТОЧНИКА: age_min считается от
      // момента, когда мы забрали число, а биржа отдаёт ход торгов инструментом
      // (юань, золото, фьючерс) без подписки на 15 минут позже. У индексов
      // delay_min = 0 — их МосБиржа отдаёт без задержки, и приписывать им чужие
      // четверть часа было бы такой же неправдой, как их скрывать.
      if (isNum(x.age_min)) {
        age = Math.max(age == null ? 0 : age,
                       x.age_min + (isNum(x.delay_min) ? x.delay_min : 0));
      }
      // intraday === false — источник отдал ЗАКРЫТИЕ вместо живой цены (откат с
      // T-Invest на историю ISS). Контракт §3 требует сказать это подписью:
      // иначе вчерашнее закрытие в шапке «Рынок сейчас» читается как текущая
      // цена, а свежий age_min это ещё и «подтверждает».
      var closed = x.intraday === false;
      return h('span', { 'class': 'bit', title: (x.label || k) + ' на ' + fmtDay(x.asof)
          + (closed ? ' — закрытие, живой цены нет' : '') }, [
        h('span', { 'class': 'bit__k', text: x.label || k }),
        h('span', { 'class': 'bit__v', text: fmtNum(x.value, QUOTE_DIGITS[k] == null ? 2 : QUOTE_DIGITS[k])
          + (closed ? ' (закрытие)' : '') }),
        isNum(x.chg_pct)
          ? h('span', { 'class': 'bit__since ' + toneOf(x.chg_pct), text: fmtNum(x.chg_pct, 2, true) + '%' })
          : null
      ]);
    });
    var word = ageWord(age == null ? null : age + (isNum(pub) && pub > 0 ? pub : 0));
    if (word) pills.push(h('span', { 'class': 'bit__since', style: 'align-self:center', text: 'цены ' + word }));
    return h('div', { 'class': 'quotes', style: 'padding:14px 20px 12px;border-bottom:1px solid var(--hair)' }, [
      h('div', { 'class': 'kicker', style: 'margin-bottom:8px', text: 'Рынок сейчас · изменение за день' }),
      h('div', { 'class': 'bits', style: 'margin-bottom:0' }, pills)
    ]);
  }

  /* ──────────────────────────────────────────────────────── позиция */

  var POSITION_WORD = { long: 'акции', flat: 'деньги' };
  // Причина последней смены словами читателя. Конвейер кладёт reason_text; код
  // причины — запас на случай витрины, где текста ещё нет.
  var REASON_WORD = {
    gate_close: 'ворота закрылись', comp_neg: 'наклон ниже порога в день решения',
    gate_open: 'ворота открылись', comp_pos: 'наклон выше порога в день решения',
    entry: 'вход: ворота открыты и наклон за акции',
    es_exit: 'цена ожиданий по ставке резко выросла', es_block: 'ждём, пока цена ожиданий по ставке успокоится',
    es_clear: 'цена ожиданий по ставке успокоилась (день решения)'
  };
  var COMP_STATE_WORD = { '1': '+ (за акции)', '-1': '− (за деньги)', '0': 'не определён' };
  var WEEKDAY = ['воскресенье', 'понедельник', 'вторник', 'среда', 'четверг', 'пятница', 'суббота'];
  function weekdayOf(iso) {
    var t = Date.parse(String(iso || ''));
    return isFinite(t) ? WEEKDAY[new Date(t).getUTCDay()] : '';
  }
  function pct2(v) { return isNum(v) ? fmtNum(v, 2, true) + '%' : '—'; }
  // Код сочетания признаков («bear|stress|stress») — внутренний ключ модели;
  // читателю он показывается словами, теми же, что на битах шапки.
  var CELL_WORD = { bull: 'бык', bear: 'медведь', stress: 'стресс', calm: 'спокойно', ok: 'ок' };
  function cellWords(code) {
    return String(code || '').split('|').filter(Boolean).map(function (w) { return CELL_WORD[w] || w; }).join(' · ');
  }
  function share(v) { return isNum(v) ? Math.round(v * 100) + '%' : '—'; }

  /* Строка позиции — первая на панели и единственный её итог: ворота ∧ наклон.
   *
   * До аудита 02.09.2026 витрина держала три разных прочтения знака (закрытый
   * месяц, дневной с гистерезисом, дневной без) и ни одного слова «позиция»:
   * читатель собирал решение сам и собирал по-разному (просадка от −26% до −34%
   * в зависимости от прочтения). Теперь позицию считает конвейер (decision.py),
   * а фронт только показывает: состояние, с какой даты и почему, ставку денег
   * во флэте, дату следующего решения по наклону и список того, что позицию
   * сменит. Тон: «акции» — акцент, «деньги» — чернила. НЕ красный и не зелёный:
   * позиция — не оценка рынка, а место, где стоят деньги.
   *
   * Старая витрина без verdict.position — законный вход: блок не рисуется. */
  function renderPosition(d) {
    var v = d.verdict || {}, p = v.position;
    if (!p || !POSITION_WORD[p.state]) return null;
    var long = p.state === 'long';
    var reason = p.reason_text || REASON_WORD[p.reason] || '';
    var st = d.states || {};
    var regimeLabel = (v.regime || st.regime || {}).label || '';
    var thr = isNum(p.comp_threshold) ? p.comp_threshold : 0.2;

    var facts = [];
    if (!long && isNum(p.cash_rate)) {
      facts.push(stat('Деньги', '≈ ' + fmtNum(p.cash_rate, 1, false) + '% годовых',
        'вклады топ-10' + (p.cash_rate_asof ? ' на ' + fmtDay(p.cash_rate_asof) : '')));
    }
    if (p.next_decision) {
      var wd = weekdayOf(p.next_decision);
      facts.push(stat('Следующее решение по наклону', ruDay(p.next_decision) + (wd ? ' (' + wd + ')' : ''),
        'порог ±' + fmtNum(thr, 1, false) + ' по дневному наклону'));
    }
    if (p.gate_open === true || p.gate_open === false) {
      facts.push(stat('Ворота', p.gate_open ? 'открыты' : 'закрыты',
        [regimeLabel || null, p.gate_since ? 'с ' + ruDay(p.gate_since) : null].filter(Boolean).join(' · ') || null));
    }
    if (isNum(p.comp_state)) {
      facts.push(stat('Знак решения', COMP_STATE_WORD[String(p.comp_state)] || '—',
        [isNum(p.comp_daily) ? 'дневной наклон ' + fmtNum(p.comp_daily, 2, true) : null,
         p.comp_state_since ? 'с ' + ruDay(p.comp_state_since) : null].filter(Boolean).join(' · ') || null));
    }
    if (isNum(p.switches_per_year)) {
      facts.push(stat('Смен в год', fmtNum(p.switches_per_year, 1, false), 'в среднем за пять лет'));
    }

    var conds = Array.isArray(p.conditions) ? p.conditions.filter(function (c) { return c; }) : [];
    var prev = v.position_prev_rule;
    var prevNote = (prev && POSITION_WORD[prev.state])
      ? ' По прежнему правилу (' + ruText(prev.rule || 'знак закрытого месяца, ворота без гистерезиса') + '): ' +
        POSITION_WORD[prev.state] + (prev.since ? ' с ' + fmtDay(prev.since) : '') + '.'
      : '';

    function sw(cls, bg) {
      var s = h('span', { 'class': 'legend__sw' + (cls ? ' ' + cls : '') });
      if (bg) s.style.background = bg;
      return s;
    }
    var legend = h('div', { 'class': 'legend', style: 'margin-top:8px' }, [
      h('span', { 'class': 'legend__i' }, [sw('legend__sw--accent'), h('span', { text: 'акции' })]),
      h('span', { 'class': 'legend__i' }, [sw(null, 'var(--mid)'), h('span', { text: 'деньги' })])
    ]);

    /* Причина в reason — это причина ПОСЛЕДНЕЙ СМЕНЫ, и со временем она перестаёт
       описывать сегодняшний день. 12.09.2026 строка читалась «деньги с 22.05 ·
       оценка рынка ушла ниже −0,2», тогда как оценка к тому дню была +0,59 уже два
       месяца, а позицию держали закрытые ворота. Рядом, в той же карточке, стояло
       «Знак решения: + (за акции)» — читатель видел противоречие.
       Поэтому к причине смены добавляется то, что держит позицию СЕЙЧАС, и только
       когда это не одно и то же. */
    var holds = '';
    if (!long) {
      if (p.gate_open === false && p.comp_state === 1) holds = 'сейчас держат закрытые ворота';
      else if (p.gate_open === true && p.comp_state === -1) holds = 'сейчас держит оценка рынка';
      else if (p.gate_open === false && p.comp_state === -1) holds = 'сейчас держат и ворота, и оценка';
    } else if (p.gate_open === true && p.comp_state === 1) {
      holds = 'ворота открыты, оценка за акции';
    }
    var since = p.since ? 'с ' + fmtDay(p.since) : '';
    var why = reason ? (since ? ' · ' : '') + ruText(reason) : '';
    var now = holds ? ((since || why) ? ' · ' : '') + holds : '';

    return h('div', { 'class': 'position position--' + p.state }, [
      h('div', { 'class': 'kicker', text: 'Позиция · итог ворот и наклона' }),
      h('div', { 'class': 'position__row' }, [
        h('span', { 'class': 'position__name', text: 'Позиция: ' + POSITION_WORD[p.state] }),
        h('span', { 'class': 'position__meta', text: since + why + now })
      ]),
      h('div', { 'class': 'position__grid' }, [
        h('div', null, [
          facts.length ? h('div', { 'class': 'stats' }, facts) : null,
          h('p', { 'class': 'position__note', text: 'Исполнять ' + ruText(p.execute || 'на следующем закрытии') +
            ' после дня решения. Ворота читаются каждый день, наклон решается по пятницам; позиция — не оценка рынка, а место, где стоят деньги.' + prevNote })
        ]),
        conds.length ? h('div', null, [
          h('div', { 'class': 'stat__k', text: 'Что изменит позицию' }),
          h('ul', { 'class': 'position__cond' }, conds.map(function (c) { return h('li', { text: ruText(c) }); }))
        ]) : null
      ]),
      Array.isArray(p.history) && p.history.length ? h('div', { 'class': 'position__ribbon' }, [
        h('div', { 'class': 'stat__k', text: 'Акции и деньги с 2004 года' }),
        C.positionRibbon(p.history, { end: p.decision_day || d.asof_trading_day, aria: 'Позиция по правилу панели с 2004 года' }),
        legend
      ]) : null
    ]);
  }

  var REGIME_QUALITY = { toxic: 'crit', stress: 'warn', calm: 'good' };
  var QUALITY_COLOR = { crit: 'var(--crit)', warn: 'var(--warn)', good: 'var(--good)', flat: 'var(--ink-3)' };

  function renderHero(d) {
    var v = d.verdict || {}, st = d.states || {}, core = d.core || {};
    var cur = st.current || {};
    // Ворота с гистерезисом (states.gate) главнее сырых битов: именно по ним
    // считается позиция, и именно они переключаются реже (вола p80/p60, RGBI
    // −4/−3%, тренд ±2% от MA200). Сырые биты остаются для ленты ячеек и их
    // статистики. Старая витрина без gate — сырые биты, как раньше.
    var gateSrc = st.gate && typeof st.gate === 'object' ? st.gate : null;
    var bitsSrc = gateSrc || cur;
    var since = (gateSrc && gateSrc.since) || cur.since || st.since || {};
    var bits = ['trend', 'vol', 'bond'].map(function (key) {
      var raw = bitsSrc[key];
      // Бит может отсутствовать: states.py кладёт null, когда у ряда нет валидных
      // значений, а при пустой панели current приходит пустым целиком. Раньше
      // «нет данных» рисовалось уверенным «медведь / спокойно» — и всегда в
      // сторону разрешения риска (снятый флаг = можно). Отсутствие показываем
      // отсутствием, как это уже делает соседний бит «Ставка».
      var known = raw === 0 || raw === 1 || raw === true || raw === false;
      if (!known) {
        return h('span', { 'class': 'bit bit--neutral' }, [
          h('span', { 'class': 'bit__dot' }),
          h('span', { 'class': 'bit__k', text: BIT_LABEL[key].k }),
          h('span', { 'class': 'bit__v', text: 'нет данных' })
        ]);
      }
      var on = raw === 1 || raw === true;
      // Возраст бита: ряд-питатель мог умереть, и last_valid отдаёт значение
      // произвольной давности как «текущее». Модель это не меняет, но подпись
      // обязана сказать, что бит стоит на старом наблюдении (> 7 суток от
      // торгового дня витрины) — иначе «облигации спокойны» читается как сегодня.
      var bitAsof = (cur.bit_asof || {})[key];
      var tradingDay = d.asof_trading_day || '';
      var bitStale = bitAsof && tradingDay &&
        (Date.parse(tradingDay) - Date.parse(bitAsof)) > 7 * 86400 * 1000;
      // Для тренда «единица» — это бык, то есть хорошо; для волы и облигаций
      // единица означает стресс. Цвет ставим по СМЫСЛУ, а не по значению бита.
      var good = key === 'trend' ? on : !on;
      var word = key === 'trend' ? (on ? BIT_LABEL.trend.on : BIT_LABEL.trend.off)
        : (on ? BIT_LABEL[key].on : BIT_LABEL[key].off);
      return h('span', { 'class': 'bit bit--' + (good ? 'off' : 'on') }, [
        h('span', { 'class': 'bit__dot' }),
        h('span', { 'class': 'bit__k', text: BIT_LABEL[key].k }),
        h('span', { 'class': 'bit__v', text: word }),
        bitStale
          ? h('span', { 'class': 'bit__since', title: 'Ряд-источник бита отстал: показано последнее наблюдение',
              text: 'данные от ' + fmtDay(bitAsof) })
          : (since[key] ? h('span', { 'class': 'bit__since', text: 'с ' + fmtDay(since[key]) }) : null)
      ]);
    });
    bits.push(h('span', { 'class': 'bit bit--neutral' }, [
      h('span', { 'class': 'bit__dot' }),
      h('span', { 'class': 'bit__k', text: 'Ставка' }),
      h('span', { 'class': 'bit__v', text: PHASE[String(cur.rate_phase)] || 'нет данных' })
    ]));

    var kicker = gateSrc ? 'Ворота · читаются ежедневно, с гистерезисом' : 'Ворота риска · что сейчас можно';
    var regime = v.regime || st.regime || null;
    var rsRaw = regime && regime.id && st.regime_stats && st.regime_stats[regime.id] ? st.regime_stats[regime.id] : null;
    // Две формы блока: вложенная {price:{…}, excess:{…}} (states.py) и плоская
    // из спецификации (mean_pct / excess_mean_pct) — читаем обе, рисуем одну.
    var rstats = rsRaw ? {
      price: rsRaw.price || { n: rsRaw.n, mean_pct: rsRaw.mean_pct, median_pct: rsRaw.median_pct,
                              hit: rsRaw.hit, ci95_pct: rsRaw.ci95_pct || rsRaw.ci95 },
      excess: rsRaw.excess || { n: rsRaw.n, mean_pct: rsRaw.excess_mean_pct, median_pct: rsRaw.excess_median_pct,
                                hit: rsRaw.excess_hit, ci95_pct: null }
    } : null;
    var left;
    if (regime && regime.label) {
      /* Три режима вместо восьми ячеек. Аудит 02.09.2026: из 28 пар ячеек
       * различима одна (по Манну–Уитни — ни одной), любое разбиение вне выборки
       * предсказывает хуже безусловного среднего. Восемь подписей с двумя знаками
       * после запятой создавали точность, которой нет; статистика режима считается
       * по закрытым месяцам и показывается двумя мерами — по цене индекса и над
       * деньгами (за вычетом ставки вкладов): решение принимается во второй. */
      var rq = REGIME_QUALITY[regime.id] || 'flat';
      var rIco = ico(rq);
      rIco.setAttribute('class', 'cellname__ico');
      rIco.style.color = QUALITY_COLOR[rq];
      var cellText = cellWords((gateSrc && gateSrc.cell_code) || v.cell_code);
      var rp = rstats ? rstats.price : null, rx = rstats ? rstats.excess : null;
      var ci = rp && rp.ci95_pct && isNum(rp.ci95_pct[0]) ? rp.ci95_pct : null;
      left = h('div', { 'class': 'hero__cell' }, [
        h('div', { 'class': 'kicker', text: kicker }),
        h('div', { 'class': 'bits' }, bits),
        h('div', { 'class': 'cellname' }, [rIco, h('span', { text: regime.label })]),
        h('div', { 'class': 'cellcode', text: 'режим из трёх' + (cellText ? ' · сочетание: ' + cellText : '') }),
        rstats ? h('div', { 'class': 'stat__k', style: 'margin-bottom:8px', text: 'Следующий месяц по цене индекса' }) : null,
        rp ? h('div', { 'class': 'stats' }, [
          stat('Медиана', pct2(rp.median_pct), 'типичный исход', toneOf(rp.median_pct)),
          stat('Среднее', pct2(rp.mean_pct), 'среднее тянут хвосты', toneOf(rp.mean_pct)),
          stat('Доля плюсовых', share(rp.hit), 'из ' + (isNum(rp.n) ? rp.n : '—') + ' закрытых месяцев')
        ]) : null,
        rx ? h('div', { 'class': 'stat__k', style: 'margin:14px 0 8px', text: 'Над деньгами · за вычетом ставки вкладов' }) : null,
        rx ? h('div', { 'class': 'stats' }, [
          stat('Медиана', pct2(rx.median_pct), 'в этой мере принимается решение', toneOf(rx.median_pct)),
          stat('Среднее', pct2(rx.mean_pct), null, toneOf(rx.mean_pct)),
          stat('Доля плюсовых', share(rx.hit), 'из ' + (isNum(rx.n) ? rx.n : '—') + ' месяцев со ставкой')
        ]) : (rp ? null : h('p', { 'class': 'empty', text: 'Статистика режима ещё не рассчитана' })),
        h('p', { 'class': 'hero__fine', text: (ci ? '95% интервал среднего по цене: ' + fmtNum(ci[0], 1, true) + '%…' + fmtNum(ci[1], 1, true) + '%. ' : '') +
          'Распределение, а не прогноз: восемь сочетаний признаков статистически неразличимы, поэтому режимов три; их таблица — в разделе «Машина состояний».' })
      ]);
    } else {
      var mean = (v.cell_stats || {}).mean_fwd1m_pct;
      var quality = !isNum(mean) ? 'flat' : (mean <= -1.5 ? 'crit' : (mean < 0.3 ? 'warn' : 'good'));
      var cellIco = ico(quality);
      cellIco.setAttribute('class', 'cellname__ico');
      cellIco.style.color = QUALITY_COLOR[quality];
      left = h('div', { 'class': 'hero__cell' }, [
        h('div', { 'class': 'kicker', text: kicker }),
        h('div', { 'class': 'bits' }, bits),
        h('div', { 'class': 'cellname' }, [cellIco, h('span', { text: v.cell_label || 'ячейка не определена' })]),
        h('div', { 'class': 'cellcode', text: cellWords(v.cell_code) }),
        /* Среднее по ячейке — ХВОСТОВАЯ статистика: у токсичной оно −2,94% при
         * медиане +0,64% и 13 плюсовых месяцах из 24. Одно среднее читается как
         * прогноз на месяц, читатель получает +5% и перестаёт верить панели. Рядом
         * со средним обязаны стоять медиана (типичный месяц) и худший месяц (то,
         * ради чего ворота закрыты). */
        h('div', { 'class': 'stats' }, [
          stat('Медиана месяца', pct2((v.cell_stats || {}).median_fwd1m_pct), 'типичный исход', toneOf((v.cell_stats || {}).median_fwd1m_pct)),
          stat('Средний форвардный месяц', pct2(mean), 'среднее тянут хвосты', toneOf(mean)),
          stat('Худший месяц', isNum((v.cell_stats || {}).worst_pct)
            ? fmtNum(v.cell_stats.worst_pct, 1, true) + '%' : '—',
            'за что закрыты ворота', toneOf((v.cell_stats || {}).worst_pct)),
          /* Доля плюсовых, медиана и края посчитаны по ЗАКРЫТЫМ месяцам, а
           * «наблюдений» — это пары исследования: у токсичной ячейки 24 против 25,
           * потому что последний месяц ещё идёт. */
          stat('Доля плюсовых', share((v.cell_stats || {}).hit),
            isNum((v.cell_stats || {}).n_closed) ? 'из ' + v.cell_stats.n_closed + ' закрытых месяцев' : null),
          stat('Наблюдений', isNum((v.cell_stats || {}).n) ? String(v.cell_stats.n) : '—',
            (isNum((v.cell_stats || {}).n_closed) && v.cell_stats.n_closed !== v.cell_stats.n)
              ? 'месяцев в ячейке; последний ещё идёт' : 'месяцев в ячейке')
        ])
      ]);
    }

    /* Подпись под наклоном строится от ПОЗИЦИИ, а не от знака дневного числа.
     * Раньше здесь жило третье, никем не документированное прочтение знака
     * («core.value > 0 → наклон вверх»), и оно спорило и с закрытым месяцем, и
     * с гистерезисом алертов. Витрина без позиции подписи не получает вовсе:
     * лучше пусто, чем ещё одно прочтение. */
    var p = v.position;
    var gateNote = null;
    if (p && POSITION_WORD[p.state]) {
      var why = p.reason_text || REASON_WORD[p.reason] || '';
      gateNote = 'Позиция — ' + POSITION_WORD[p.state] + (why ? ': ' + ruText(why) : '') +
        '. Наклон читается в день решения, ворота — каждый день.';
    }
    var me = core.month_end || {};
    var thr = p && isNum(p.comp_threshold) ? p.comp_threshold : 0.2;

    var right = h('div', { 'class': 'hero__gauge' }, [
      h('div', { 'class': 'kicker', text: 'Наклон · куда смотрит ядро' }),
      h('div', { 'class': 'gaugeval' }, [
        h('span', {
          'class': 'gaugeval__n ' + toneOf(core.value),
          style: 'font-size:clamp(22px,3.4vw,34px)',
          text: fmtNum(core.value, 2, true)
        }),
        h('span', { 'class': 'gaugeval__l', text: v.core_label || '' })
      ]),
      gateNote ? h('div', { 'class': 'gaugeval__l', style: 'margin:6px 0 10px', text: gateNote }) : null,
      C.polarityScale(core.value),
      // Под шкалой — траектория за два года: одно значение не отвечает на вопрос
      // «композит разворачивается или затухает», а место под шкалой всё равно пустует.
      h('div', { 'class': 'gauge__trail' }, [
        h('div', { 'class': 'stat__k', text: 'Композит за 24 месяца' }),
        C.signedHistory((core.series || []).slice(-24), {
          height: 84, label: 'композит', aria: 'Композит ядра за последние 24 месяца'
        })
      ]),
      h('div', { 'class': 'gauge__meta' }, [
        // Решение по наклону — раз в неделю по ДНЕВНОМУ значению с порогом ±0,2
        // (аудит 02.09.2026, ступень P1): закрытый месяц больше не решает, но
        // остаётся справочной строкой мелко — им сверяют, куда складывается месяц.
        p ? h('div', { text: 'решение по наклону — по пятницам (последний торговый день недели), порог ±' + fmtNum(thr, 1, false) }) : null,
        p && isNum(p.comp_state) ? h('div', { text: 'знак решения: ' + (COMP_STATE_WORD[String(p.comp_state)] || '—') +
          (p.comp_state_since ? ' с ' + fmtDay(p.comp_state_since) : '') +
          (isNum(p.comp_daily) ? ' · дневной ' + fmtNum(p.comp_daily, 2, true) + (p.decision_day ? ' на ' + fmtDay(p.decision_day) : '') : '') }) : null,
        !p ? h('div', { text: core.sign_since ? 'знак не менялся с ' + fmtDay(core.sign_since) : 'знак ещё не определялся' }) : null,
        isNum(me.value) ? h('div', {
          'class': p ? 'gauge__fine' : null,
          title: p ? 'Справочно: значение на последнем закрытом месяце; решение принимается по дневному значению в день решения'
            : 'Ребаланс мышления месячный: внутримесячные колебания композита решения не меняют',
          text: 'последний закрытый месяц: ' + fmtNum(me.value, 2, true) +
            (me.label ? ' (' + me.label + ')' : '') + (me.date ? ' на ' + fmtDay(me.date) : '')
        }) : null
      ])
    ]);

    var ruleIco = ico('info', 'rule__ico');
    return h('div', { 'class': 'hero' }, [
      renderQuotes(d),
      renderPosition(d),
      h('div', { 'class': 'hero__grid' }, [left, right]),
      v.rule ? h('div', { 'class': 'rule' }, [
        ruleIco,
        h('div', null, [
          h('div', { 'class': 'rule__k', text: 'Правило дня' }),
          h('p', { 'class': 'rule__t', text: ruText(v.rule) })
        ])
      ]) : null
    ]);
  }

  /* ────────────────────────────────────────────────────────── ядро */

  function renderCore(d) {
    var core = d.core || {};
    var comps = core.components || [];
    var slots = ['var(--s1)', 'var(--s2)', 'var(--s3)'];

    var cards = comps.map(function (c, i) {
      var color = slots[i % slots.length];
      var mark = h('span', { 'class': 'comp__mark' });
      mark.style.background = color;
      return h('article', { 'class': 'card comp' }, [
        h('div', { 'class': 'comp__top' }, [
          mark,
          h('div', null, [
            h('div', { 'class': 'comp__label', text: c.label || c.id }),
            h('div', { 'class': 'comp__z' }, [
              // Крупно — ВКЛАД в композит (знак × z), а не сырой z: у ноги с
              // отрицательным знаком положительный z толкает композит ВНИЗ, и
              // «+0,41» рядом с синим цветом читалось как противоречие.
              h('span', {
                'class': 'comp__zn ' + toneOf(isNum(c.z) ? c.z * (c.sign || 1) : null),
                text: fmtNum(isNum(c.z) ? c.z * (c.sign || 1) : null, 2, true),
                title: 'Вклад в композит: знак компонента × z-скор'
              }),
              h('span', { 'class': 'comp__raw', text: (isNum(c.z) ? 'z ' + fmtNum(c.z, 2, true) + ' · ' : '') + ruText(c.raw_fmt) })
            ])
          ])
        ]),
        C.spark(c.spark, tok(i === 0 ? '--s1' : (i === 1 ? '--s2' : '--s3')),
          { aria: 'z-скор «' + (c.label || c.id) + '» за два года' }),
        h('p', { 'class': 'comp__mech', text: ruText(c.mechanism) }),
        h('div', { 'class': 'comp__foot' }, [
          tier(c.tier),
          h('span', { text: 'вес ' + (isNum(c.weight) ? Math.round(c.weight * 100) + '%' : '—') }),
          c.protected === false ? h('span', { title: 'Нога не переживает поправку на множественность — держим с оговоркой', text: '· не защищена' }) : null
        ])
      ]);
    });

    var hist = h('article', { 'class': 'card' }, [
      h('div', { 'class': 'card__head' }, [
        h('h3', { 'class': 'card__title', text: 'Композит с 2004 года' }),
        h('span', { 'class': 'card__note', text: 'заливка — знак; точки на нуле — смены знака' })
      ]),
      C.signedHistory(core.series, { height: 200, label: 'композит', aria: 'Композит ядра' })
    ]);

    var hl = core.health || {};
    /* Статусы после аудита 02.09.2026: ok — связь видна; warn — связи на этом окне
       не видно (и это НЕ «модель сломана»: при окне 24 месяца интервал ±0,41, а
       слом быстрее ~5 лет статистически не обнаруживается); review — двенадцать
       закрытых месяцев подряд ниже нуля, плановая ревалидация состава. Старые
       витрины присылают dead — читаем его как warn. */
    var hlWord = { ok: 'связь видна', warn: 'связи не видно', review: 'ревалидация', dead: 'связи не видно' };
    var hlStatus = hlWord[hl.status] || 'нет данных';
    var hlKind = { ok: 'good', warn: 'warn', review: 'crit', dead: 'warn' }[hl.status] || 'flat';
    var hlIco = ico(hlKind);
    hlIco.setAttribute('class', 'sig__ico');
    hlIco.style.color = QUALITY_COLOR[hlKind];
    var reviewMonths = isNum(hl.review_months) ? hl.review_months : 12;
    var coversZero = hl.ic_ci95 && isNum(hl.ic_ci95[0]) && hl.ic_ci95[0] < 0 && hl.ic_ci95[1] > 0;
    var hlFoot;
    if (hl.review_due || hl.status === 'review') {
      hlFoot = 'IC ниже нуля ' + (hl.below_zero_months || 0) + ' мес подряд — порог плановой ревалидации состава (' +
        reviewMonths + ' месяцев, §7) достигнут. Протокол — реколибровка, а не сокращение позиции: на истории такие тревоги ' +
        'были контрарными (после них умение модели и избыток над деньгами выше среднего).';
    } else if (hl.status === 'warn' || hl.status === 'dead') {
      hlFoot = (isNum(hl.ic_24m) && hl.ic_24m < 0 ? 'IC ниже нуля' : 'IC около нуля') +
        (coversZero ? ', и доверительный интервал накрывает ноль: на 24 месяцах отличить модель от монетки нечем. ' : '. ') +
        '«Связи не видно» — не «модель сломана»: слом быстрее пяти лет статистически не обнаруживается. ' +
        'Ревалидация состава — после ' + reviewMonths + ' месяцев подряд ниже нуля (сейчас ' + (hl.below_zero_months || 0) + '); ' +
        'до этого состав не меняется.';
    } else {
      hlFoot = 'Состав ядра фиксирован: отбор по скользящей результативности проверялся на истории и проиграл.';
    }

    var health = h('article', { 'class': 'card' }, [
      h('div', { 'class': 'card__head' }, [
        h('h3', { 'class': 'card__title', text: 'Здоровье модели' }),
        h('span', { 'class': 'card__note', text: 'скользящий ранговый IC за 24 месяца' })
      ]),
      h('div', { 'class': 'health' }, [
        /* Интервал ПОД самим числом, а не в примечании: при окне 24 месяца он
           шириной около ±0,41, то есть шире любого значения, какое здесь может
           стоять. Без него «−0,08» читается как приговор модели, тогда как это
           «информации нет» — отчёт реколибровки печатает интервал давно, а живая
           карточка не печатала (оплачено тревогой 01.09.2026). */
        stat('IC 24 мес', fmtNum(hl.ic_24m, 2, true),
          (hl.ic_ci95 && isNum(hl.ic_ci95[0]))
            ? '95% интервал ' + fmtNum(hl.ic_ci95[0], 2, true) + '…' + fmtNum(hl.ic_ci95[1], 2, true)
            : null,
          toneOf(hl.ic_24m)),
        stat('Наблюдений', isNum(hl.n) ? String(hl.n) : '—'),
        isNum(hl.below_zero_months) ? stat('Ниже нуля подряд',
          hl.below_zero_months + ' мес',
          'порог ревалидации — ' + reviewMonths,
          hl.review_due ? 'tone-neg' : 'tone-mut') : null,
        h('div', null, [
          h('div', { 'class': 'stat__k', text: 'Статус' }),
          h('div', { 'class': 'sig__verdict', style: 'margin-top:2px' }, [hlIco, h('span', { text: hlStatus })])
        ])
      ]),
      hl.series ? C.miniSeries(hl.series, {
        height: 44, zero: true, digits: 2, label: 'IC',
        color: tok('--ink-3'), aria: 'Скользящий IC модели по месяцам'
      }) : null,
      h('p', { 'class': 'card__foot', text: hlFoot })
    ]);

    return section('Ядро', 'Слой 1 · медленный композит, меняет знак примерно дважды в год', [
      h('div', { 'class': 'comps', style: 'margin-bottom:16px' }, cards),
      h('div', { 'class': 'grid', style: 'grid-template-columns:1fr' }, [hist, health])
    ]);
  }

  /* ────────────────────────────────────────────── машина состояний */

  function renderStates(d) {
    var st = d.states || {};
    var cells = st.cells || [];

    var legendSteps = [
      { c: 'var(--neg)', t: 'ниже −1,5%/мес' },
      { c: 'color-mix(in srgb, var(--neg) 45%, var(--mid))', t: 'от −1,5% до 0' },
      { c: 'var(--mid)', t: 'около нуля' },
      { c: 'color-mix(in srgb, var(--pos) 45%, var(--mid))', t: 'от +0,8% до +1,8%' },
      { c: 'var(--pos)', t: 'выше +1,8%/мес' }
    ];
    var legend = h('div', { 'class': 'legend' }, legendSteps.map(function (s) {
      var sw = h('span', { 'class': 'legend__sw' });
      sw.style.background = s.c;
      return h('span', { 'class': 'legend__i' }, [sw, h('span', { text: s.t })]);
    }));

    var ribbon = h('article', { 'class': 'card' }, [
      h('div', { 'class': 'card__head' }, [
        h('h3', { 'class': 'card__title', text: 'Лента ячеек с 2004 года' }),
        h('span', { 'class': 'card__note', text: 'цвет — средняя форвардная доходность ячейки по сырым признакам; ▾ — сейчас' })
      ]),
      C.stateRibbon(st.series, cells),
      legend
    ]);

    /* Восемь сочетаний остаются здесь таблицей, а не в шапке: числа исследования
     * заморожены (CELL_STATS), но попарно неразличимы, и в шапке их место занял
     * режим из трёх. Текущее сочетание помечено — по сырым признакам, как и
     * лента: это статистика исследования, а не ворота позиции. */
    var cellRows = cells.slice().sort(function (a, b) {
      return (isNum(b.mean_fwd1m_pct) ? b.mean_fwd1m_pct : -99) - (isNum(a.mean_fwd1m_pct) ? a.mean_fwd1m_pct : -99);
    });
    var cellsCard = cellRows.length ? h('article', { 'class': 'card', style: 'margin-top:16px' }, [
      h('div', { 'class': 'card__head' }, [
        h('h3', { 'class': 'card__title', text: 'Восемь сочетаний признаков' }),
        h('span', { 'class': 'card__note', text: 'месячные данные 2004–2026; попарно неразличимы, поэтому в шапке — режим из трёх' })
      ]),
      h('div', { 'class': 'tablewrap' }, [
        h('table', { 'class': 'data' }, [
          h('caption', { text: 'Средний следующий месяц по цене индекса и доля плюсовых месяцев в каждом сочетании трёх признаков. Строка «сейчас» — по сырым признакам, без гистерезиса.' }),
          h('thead', null, [h('tr', null, [
            h('th', { scope: 'col', text: 'Сочетание' }), h('th', { scope: 'col', text: 'Название' }),
            h('th', { scope: 'col', text: 'Средний месяц' }), h('th', { scope: 'col', text: 'Доля плюсовых' }),
            h('th', { scope: 'col', text: 'Наблюдений' })
          ])]),
          h('tbody', null, cellRows.map(function (c) {
            return h('tr', { 'class': c.current ? 'is-current' : null }, [
              h('th', { scope: 'row', text: cellWords(c.code) + (c.current ? ' ◂ сейчас' : '') }),
              h('td', { text: c.label || '—' }),
              h('td', { 'class': toneOf(c.mean_fwd1m_pct), text: pct2(c.mean_fwd1m_pct) }),
              h('td', { text: share(c.hit) }),
              h('td', { text: isNum(c.n) ? String(c.n) : '—' })
            ]);
          }))
        ])
      ])
    ]) : null;

    // Расстояние до переключения считается от ТОГО порога, который сработает
    // следующим: у включённого бита это порог выключения (вола p60, RGBI −3%,
    // тренд −2% от MA200), у выключенного — порог включения. Витрина без
    // гистерезиса (старый data.json) присылает один порог — берём его.
    var gateSrc = st.gate && typeof st.gate === 'object' ? st.gate : (st.current || {});
    var dists = (st.distances || []).map(function (x) {
      var bit = gateSrc[x.id];
      var on = bit === 1 || bit === true;
      var useOff = on && isNum(x.off_threshold);
      var thr = useOff ? x.off_threshold : (isNum(x.on_threshold) ? x.on_threshold : x.threshold);
      var text = useOff ? (x.text_off || x.text) : x.text;
      var which = x.id === 'trend' ? (on ? 'смена на медведя' : 'смена на быка') : (on ? 'снятие флага' : 'включение флага');
      return h('div', { 'class': 'dist' }, [
        h('div', { 'class': 'dist__row' }, [
          h('span', { 'class': 'dist__k', text: x.label || x.id }),
          h('span', { 'class': 'dist__v', text: fmtNum(x.value, 1, true) + ' → ' + fmtNum(thr, 1, true) })
        ]),
        // Полярность объявлена у ВСЕХ трёх строк, как требует контракт thresholdBar
        // (charts.js: «либо у всех, либо ни у одной»). Тренд: плохо НИЖЕ MA200;
        // облигации: плохо ГЛУБЖЕ порога просадки; волатильность: плохо ВЫШЕ порога.
        C.thresholdBar(x.value, thr, { bad: x.id === 'vol' ? 'above' : 'below' }),
        h('p', { 'class': 'dist__t', text: ruText(text) + (isNum(x.off_threshold) ? ' · порог: ' + which : '') })
      ]);
    });

    var sigs = (st.active_signals || []).map(function (s) {
      var pos = /за лонг/.test(s.verdict || '');
      var neg = /против/.test(s.verdict || '');
      var vIco = ico(pos ? 'up' : (neg ? 'down' : 'flat'), 'sig__ico');
      vIco.style.color = pos ? 'var(--pos)' : (neg ? 'var(--neg)' : 'var(--ink-3)');
      return h('div', { 'class': 'sig' }, [
        h('div', { 'class': 'sig__row' }, [
          h('span', { 'class': 'sig__k', text: s.label || s.id }),
          h('span', {
            'class': 'sig__z ' + toneOf(isNum(s.z) ? s.z * (s.sign || 1) : null),
            title: 'Вклад в вердикт: знак сигнала × z-скор',
            text: isNum(s.z) ? fmtNum(s.z * (s.sign || 1), 2, true) : '—' })
        ]),
        h('div', { 'class': 'sig__verdict' }, [
          vIco, h('span', { text: ruText(s.verdict) || 'нет данных' }),
          // Число подписывается СВОЕЙ датой, а не словом «сейчас»: часть рядов
          // структурно отстаёт (позиция физлиц без подписки — до двух недель, плюс
          // протяжка), и «сейчас +0,57» выдавало двухнедельную позицию за
          // сегодняшнюю. asof/lag_days пайплайн кладёт ровно для этой подписи
          // (CONTRACT §3: «фронт обязан показать дату, при большом lag_days —
          // бейджем»).
          isNum(s.value) ? h('span', { 'class': 'tone-mut',
            text: '· z ' + fmtNum(s.z, 2, true) + ', ' + fmtNum(s.value, 2, true)
              + (s.asof ? ' на ' + ruDay(s.asof) : '') }) : null,
          (isNum(s.lag_days) && s.lag_days > 5) ? h('span', {
            'class': 'sig__lag',
            title: 'Возраст показанного числа: источник отстаёт структурно',
            text: 'данные отстают на ' + s.lag_days + ' дн.' }) : null
        ]),
        h('p', { 'class': 'sig__why', text: ruText(s.why) })
      ]);
    });

    return section('Машина состояний', 'Слой 2 · ворота риска, восемь сочетаний и сигналы второго ряда', [
      ribbon,
      h('div', { 'class': 'two', style: 'margin-top:16px' }, [
        h('article', { 'class': 'card' }, [
          h('div', { 'class': 'card__head' }, [
            h('h3', { 'class': 'card__title', text: 'Расстояние до переключения' }),
            gateSrc === st.gate ? h('span', { 'class': 'card__note', text: 'до порога, который сработает следующим' }) : null
          ]),
          dists.length ? h('div', null, dists) : h('p', { 'class': 'empty', text: 'Расстояния ещё не рассчитаны' })
        ]),
        h('article', { 'class': 'card' }, [
          h('div', { 'class': 'card__head' }, [
            h('h3', { 'class': 'card__title', text: 'Активные сигналы второго ряда' }),
            h('span', { 'class': 'card__note', text: 'объясняют, а не голосуют; вердикт считается по z-скору' })
          ]),
          sigs.length ? h('div', null, sigs)
            : h('p', { 'class': 'empty', text: 'В текущем сочетании сигналы второго ряда не включены' })
        ])
      ]),
      cellsCard
    ]);
  }

  /* ───────────────────────────────────────────────────── мониторы */

  function tileBody(m) {
    var p = m.payload || {};
    var out = [];
    function num(v, digits, unit, sign) {
      return h('div', { 'class': 'tile__num ' + (sign ? toneOf(v) : ''), text: fmtNum(v, digits, sign) + (unit || '') });
    }
    switch (m.id) {
      case 'expectations':
        // Крупно — спред годовой ОФЗ к ключу: это и есть «сколько смягчения в
        // цене». Рядом три числа того же тайла: изменение спреда за 21 день
        // (рост больше +0,25 п.п. — детектор турбулентности, живёт в тени),
        // RUSFAR 3M − ключ и полгода ОФЗ − ключ. Уровень направление не
        // предсказывает — тир B, не решение.
        var y1k = isNum(p.spread_y1_key_pp) ? p.spread_y1_key_pp : p.y1_minus_key;
        var d21 = isNum(p.chg_21d_pp) ? p.chg_21d_pp : p.d21;
        var rk = isNum(p.spread_rusfar_key_pp) ? p.spread_rusfar_key_pp : p.rusfar_minus_key;
        var y05k = isNum(p.spread_y05_key_pp) ? p.spread_y05_key_pp : p.y05_minus_key;
        var y1v = isNum(p.y1_pct) ? p.y1_pct : p.y1, keyv = isNum(p.key_rate_pct) ? p.key_rate_pct : p.key_rate;
        out.push(num(y1k, 2, ' п.п.', true));
        out.push(h('div', { 'class': 'tile__sub', text: 'год ОФЗ минус ключ' +
          ((isNum(y1v) && isNum(keyv)) ? ' (' + fmtNum(y1v, 2, false) + '% против ' + fmtNum(keyv, 2, false) + '%)' : '') +
          (isNum(d21) ? '; за 21 день ' + fmtNum(d21, 2, true) + ' п.п.' : '') +
          (p.repricing === true ? ' — рост больше порога, репрайсинг ожиданий (тень)' : '') }));
        if (isNum(rk) || isNum(y05k)) {
          out.push(h('div', { 'class': 'tile__sub', text: [
            isNum(rk) ? 'RUSFAR 3M − ключ ' + fmtNum(rk, 2, true) + ' п.п.' : null,
            isNum(y05k) ? 'полгода ОФЗ − ключ ' + fmtNum(y05k, 2, true) + ' п.п.' : null
          ].filter(Boolean).join(' · ') }));
        }
        if (p.series) out.push(C.miniSeries(p.series, { digits: 2, zero: true, label: 'год − ключ', unit: ' п.п.', color: tok('--s2') }));
        break;
      case 'orfr':
        if (p.stack && p.months) {
          var fiz = (p.stack.fiz || []);
          var last = p.last || {};
          var lastMon = p.months.length ? fmtMon(p.months[p.months.length - 1]) : '';
          // Тайл не показывал НИ ОДНОГО числа, а подпись отправляла за цифрами
          // управляющих и банков в подсказку, которой там нет: подсказка ленты
          // знает только физлиц, а с телефона hover недостижим вовсе. Поэтому
          // крупно — то же, что в столбиках (физлица), а все шесть категорий
          // за последний месяц выводим текстом: это единственный способ прочитать
          // рекордный отток ДУ без мыши.
          if (isNum(last.fiz)) out.push(num(last.fiz, 1, ' млрд ₽', true));
          out.push(C.flowBars(p.months, fiz, { label: 'физлица, нетто', unit: ' млрд', aria: 'Нетто-покупки акций физлицами' }));
          var labels = p.labels || {};
          var parts = Object.keys(labels).filter(function (k) { return isNum(last[k]); }).map(function (k) {
            return labels[k] + ' ' + fmtNum(last[k], 1, true);
          });
          out.push(h('div', { 'class': 'tile__sub', text: 'Столбики — нетто физлиц по месяцам.' +
            (parts.length ? ' ' + lastMon + ', млрд ₽: ' + parts.join(' · ') : '') }));
        }
        break;
      case 'lqdt':
        out.push(num(p.aum, 0, ' млрд ₽'));
        out.push(h('div', { 'class': 'tile__sub', text: p.rotation_started ? 'Ротация началась' : 'Большой ротации ещё не случалось' }));
        break;
      case 'deposit_spread':
        out.push(num(p.spread_pp, 1, ' п.п.', true));
        // Обе доходности — с одним знаком после запятой: их тут же вычитают друг
        // из друга, и «12,85% против 8,5%» читалось как небрежность в паре чисел,
        // из которых собран спред строкой выше.
        out.push(h('div', { 'class': 'tile__sub', text: 'Вклады ' + fmtNum(p.deposit_pct, 1, false) + '% против дивидендов ' + fmtNum(p.dy_trail_pct, 1, false) + '%' }));
        if (p.series) out.push(C.miniSeries(p.series, { digits: 1, zero: true, label: 'спред', unit: ' п.п.', color: tok('--s1') }));
        break;
      case 'cb_meeting':
        if (!isNum(p.days_left)) {
          // Календарь заседаний кончился (после 18.12.2026 до ручного обновления
          // константы): «— дн.» ничего не объясняет, а конвейер уже положил
          // честный заголовок «календарь закончился, нужен новый» — показываем его.
          out.push(h('div', { 'class': 'tile__sub', text: ruText(m.headline || 'календарь заседаний закончился — нужен новый') }));
          break;
        }
        out.push(num(p.days_left, 0, ' дн.'));
        out.push(h('div', { 'class': 'tile__sub', text: 'до заседания ' + fmtDay(p.next_meeting) + '; ключевая ' + fmtNum(p.key_rate, 2, false) + '%' +
          (isNum(p.consensus) ? ', консенсус ' + fmtNum(p.consensus, 2, false) + '%' : ', консенсус не внесён') }));
        // Недельная инфляция переехала сюда одной строкой: отдельный тайл снят
        // аудитом 02.09.2026 (для акций предиктивности нет, для ожиданий по
        // ставке — контекст). Имя поля выбирает конвейер: печатаем любое
        // строковое поле payload, начинающееся с cpi.
        Object.keys(p).forEach(function (k) {
          if (/^cpi/.test(k) && typeof p[k] === 'string' && p[k]) out.push(h('div', { 'class': 'tile__sub', text: ruText(p[k]) }));
        });
        break;
      case 'polymarket':
        out.push(num(p.prob_pct, 0, '%'));
        /* Горизонт печатается рядом с числом, а не только в примечании:
           вероятность «до даты» тем ниже, чем ближе дата, и без срока 2% по
           августовскому контракту и 24% по декабрьскому читаются как
           противоречие, хотя это один рынок в один день. */
        out.push(h('div', { 'class': 'tile__sub', text: (isNum(p.chg_7d_pp) ? fmtNum(p.chg_7d_pp, 1, true) + ' п.п. за неделю. ' : '') +
          (isNum(p.horizon_days) ? 'горизонт ' + p.horizon_days + ' дн. · ' : '') + (p.question || '') }));
        if (p.series) out.push(C.miniSeries(p.series, { digits: 0, unit: '%', label: 'вероятность', color: tok('--s2') }));
        break;
      case 'futoi':
        out.push(num(p.z120, 2, '', true));
        // Крупное число — z относительно 120-дневной нормы, а не уровень позиции:
        // при z −2,93 физлица в этой витрине нетто-ДЛИННЫЕ (+10,1% от брутто).
        // Рядом с нормировкой печатаем сам уровень, чтобы «минус» на тайле не
        // читался как «физики в шорте».
        out.push(h('div', { 'class': 'tile__sub', text: 'z нетто-позиции физлиц за 120 дней' +
          (isNum(p.net_share) ? '; сейчас нетто ' + fmtNum(p.net_share * 100, 1, true) + '% от брутто' : '') +
          // holders_* — число ЛИЦ (unit=persons), а не контракты: «лонгов 70 000»
          // рядом с z нетто-позиции читалось как объём длинных позиций.
          '; держателей лонга ' + fmtNum(p.holders_long, 0, false) + ', шорта ' + fmtNum(p.holders_short, 0, false) }));
        if (p.series) out.push(C.miniSeries(p.series, { digits: 2, zero: true, label: 'нетто/брутто', color: tok('--s3') }));
        break;
      case 'rub_barrel':
        out.push(num(p.tax_barrel_rub, 0, ' ₽'));
        // Тире перед знаковым числом давало «5 440 ₽ — −13%»: пара «— −» читается
        // как двойное тире, а не как «столько-то ниже».
        out.push(h('div', { 'class': 'tile__sub', text: 'налоговая бочка против бюджетных ' +
          fmtNum(p.budget_barrel_rub, 0, false) + ' ₽, ' + fmtNum(p.gap_pct, 0, true) + '%' }));
        break;
      case 'breadth':
        out.push(num(p.pct_above_ma200, 0, '%'));
        out.push(h('div', { 'class': 'tile__sub', text: 'бумаг выше 200-дневной' +
          (isNum(p.chg_21d_pp) ? '; ' + fmtNum(p.chg_21d_pp, 0, true) + ' п.п. за месяц' : '') }));
        if (p.series) out.push(C.miniSeries(p.series, { digits: 0, unit: '%', label: 'ширина', color: tok('--s1') }));
        break;
      case 'hy_spread':
        out.push(num(p.spread_pp, 1, ' п.п.'));
        out.push(h('div', { 'class': 'tile__sub', text: 'ВДО ' + fmtNum(p.hy_yield, 1, false) + '% к ' + (p.base_label || 'ОФЗ') }));
        if (p.series) out.push(C.miniSeries(p.series, { digits: 1, label: 'спред', unit: ' п.п.', color: tok('--s2') }));
        break;
      case 'rvi':
        out.push(num(p.rvi, 1, ''));
        out.push(h('div', { 'class': 'tile__sub', text: isNum(p.pct_3y) ? pctile(p.pct_3y) + '-й перцентиль за 3 года' : '' }));
        if (p.series) out.push(C.miniSeries(p.series, { digits: 1, label: 'RVI', color: tok('--ink-3') }));
        break;
      case 'mcxsm':
        out.push(num(p.rs_63d_pct, 1, '%', true));
        out.push(h('div', { 'class': 'tile__sub', text: 'малые каппы против индекса за 63 дня' }));
        if (p.series) out.push(C.miniSeries(p.series, { digits: 2, label: 'отношение', color: tok('--s3') }));
        break;
      case 'cpi_weekly':
        out.push(num(p.last_pct, 2, '%', true));
        // Не «SAAR»: сезонной корректировки в расчёте нет, а буквы SA её обещают.
        out.push(h('div', { 'class': 'tile__sub', text: 'недельный принт; в годовом выражении по 4 неделям ' + fmtNum(p.annualized_4w_pct, 1, false) + '% (без сезонной корректировки)' }));
        break;
      case 'ofz_auctions':
        // Провал аукциона — это не «разместили ноль»: крупное «0,0 млрд ₽» ничем
        // не отличалось от состоявшегося размещения на нулевую сумму. Пишем словом.
        if (p.failed) {
          out.push(h('div', { 'class': 'tile__headline', text: 'аукцион ' + fmtDay(p.date) + ' не состоялся' }));
        } else {
          out.push(num(p.placed_bn, 1, ' млрд ₽'));
          out.push(h('div', { 'class': 'tile__sub', text: 'спрос ' + fmtNum(p.demand_bn, 1, false) +
            ' млрд, bid-to-cover ' + fmtNum(p.bid_to_cover, 2, false) }));
        }
        break;
      case 'sep_node':
        out.push(h('div', { 'class': 'tile__headline', text: ruText(m.headline) }));
        break;
      case 'retail':
        // Крупным — долю в обороте: именно она объясняет, почему поток розницы
        // вообще что-то значит для индекса. Число счетов сюда не годится —
        // 41,9 млн открытых счетов и 3,0 млн торгующих это разные величины,
        // и крупная цифра «41,9 млн» читалась бы как число участников рынка.
        out.push(num(p.share_equity_pct, 0, '%'));
        out.push(h('div', { 'class': 'tile__sub', text: 'оборота акций за физлицами; активны ' +
          fmtNum(p.active_mln, 1, false) + ' из ' + fmtNum(p.clients_total_mln, 1, false) + ' млн счетов' +
          (isNum(p.inflow_equity_bln) ? '; в акции ' + fmtNum(p.inflow_equity_bln, 1, true) + ' млрд ₽' : '') }));
        if (p.portfolio && p.portfolio.length) {
          out.push(h('div', { 'class': 'tile__sub', text: 'народный портфель: ' +
            p.portfolio.slice(0, 3).map(function (x) {
              return ruText(x.name) + ' ' + fmtNum(x.share_pct, 0, false) + '%';
            }).join(', ') }));
        }
        break;
      default:
        // «Нет данных» тайл и так скажет в подвале датой, а monitor-заметка — словами.
        // Три сообщения об одном факте в карточке высотой в два экрана телефона —
        // это шум, поэтому пустой заголовок при status=missing не печатаем.
        if (m.status !== 'missing') out.push(h('div', { 'class': 'tile__headline', text: ruText(m.headline) || 'нет данных' }));
    }
    if (!out.length && m.status !== 'missing') {
      out.push(h('div', { 'class': 'tile__headline', text: ruText(m.headline) || 'нет данных' }));
    }
    return out;
  }

  // Поля payload, в которых у тайлов лежит настоящая дата данных: нужны, когда
  // в asof тайла стоит дата события, а не наблюдения.
  var DATA_ASOF_KEYS = ['key_rate_asof', 'rusfar_asof', 'deposit_asof', 'dy_asof', 'proxy_asof'];

  /* Ярлык «данные: …» обязан означать одно и то же во всей сетке.
   *
   * У тайла «Заседание ЦБ» конвейер кладёт в asof дату БУДУЩЕГО заседания
   * (11.09.2026 при витрине от 11.08.2026) — и подпись начинала врать: у
   * четырнадцати тайлов это дата наблюдения, у одного дата события, а свежесть
   * такого тайла не может протухнуть по определению. Дату из будущего в ярлык
   * не пускаем: берём настоящую дату данных из payload, а если её нет — молчим.
   */
  function tileAsof(m, d) {
    // Горизонт наблюдения — день СБОРКИ витрины, а не торговый день модели.
    // asof_trading_day отстаёт по определению: ядро и состояния считаются по
    // ЗАКРЫТИЮ, поэтому внутри дня он вчерашний. Сравнение с ним объявляло «нет
    // данных» о четырёх тайлах со свежайшими СЕГОДНЯШНИМИ данными — дивидендный
    // календарь, вероятность перемирия, позиции физлиц, бюджетный узел.
    // В будущее по-прежнему не пускаем: дата события (заседание через месяц)
    // отсеивается как и раньше.
    //
    // День сборки считается по МОСКВЕ, а не по UTC. Тайлы датируют себя московским
    // днём (compute/monitors.py: _msk_now), и на такте после 21:00 UTC московская дата
    // уже завтрашняя — сравнение с UTC-датой объявляло свежие данные «будущими» и
    // прятало подпись до утра. Ловилось у бюджетного узла на закрывающем такте 00:00 МСК.
    // МСК фиксирован (UTC+3) круглый год, поэтому сдвиг — константа, как и в fmtStamp.
    var built = Date.parse(d.generated_at);
    var horizon = isFinite(built)
      ? new Date(built + 3 * 3600 * 1000).toISOString().slice(0, 10)
      : (d.asof_trading_day || '');
    var asof = m.asof ? String(m.asof) : null;
    if (asof && horizon && asof.slice(0, 10) > horizon) {
      var p = m.payload || {}, best = null;
      DATA_ASOF_KEYS.forEach(function (k) {
        var v = p[k];
        if (typeof v === 'string' && v.slice(0, 10) <= horizon && (!best || v > best)) best = v;
      });
      asof = best;
    }
    if (!asof) return 'нет данных';
    return 'данные: ' + (asof.length > 7 ? fmtDay(asof) : fmtMon(asof + '-01'));
  }

  function renderMonitors(d) {
    var tiles = (d.monitors || []).map(function (m) {
      var body = tileBody(m);
      var note = ruText(trimNote(m.note));
      return h('article', { 'class': 'card tile' + (m.tier === 'dead' ? ' tile--dead' : '') }, [
        h('div', { 'class': 'tile__head' }, [
          h('h3', { 'class': 'tile__title', text: m.title || m.id }),
          h('span', { 'class': 'tile__head-r' }, [tier(m.tier), statusDot(m.status)])
        ])
      ].concat(body, [
        note ? h('p', { 'class': 'tile__sub', text: note }) : null,
        h('div', { 'class': 'tile__foot' }, [h('span', { text: tileAsof(m, d) })])
      ]));
    });
    return section('Мониторы', 'Слой 3 · наблюдение без предиктивных претензий', [
      tiles.length ? h('div', { 'class': 'tiles' }, tiles) : h('p', { 'class': 'empty', text: 'Тайлы ещё не собраны' })
    ]);
  }

  /* ──────────────────────────────────────────────────────────── тень */

  var SHADOW_STATUS = {
    error: 'сигнал не посчитался', no_series: 'ряда в сторе нет', no_data: 'данных пока мало', warming: 'ряд ещё прогревается',
    accumulating: 'накапливаем историю', unavailable: 'недоступна'
  };
  var SHADOW_GROUP = {
    rates: 'ставки', legs: 'теневая нога', flows: 'потоки', gate: 'ворота', retail_era: 'розничная эра'
  };

  /* Тень: сигналы, которые аудит 02.09.2026 оставил под наблюдение на 12 месяцев.
   * Считаются и копят историю вне выборки, но на позицию НЕ влияют — поэтому
   * карточки пунктирные, а состояние бита набрано чернилами, не статусным
   * цветом: у тени нет права выглядеть сигналом. Витрина без d.shadow — законный
   * вход: раздел не рисуется. Ошибка одного сигнала приходит его же статусом. */
  function renderShadow(d) {
    var sh = d.shadow;
    if (!sh || typeof sh !== 'object') return null;
    var sigs = Array.isArray(sh.signals) ? sh.signals : [];
    var sp = sh.shadow_position && typeof sh.shadow_position === 'object' ? sh.shadow_position : null;
    var spOk = sp && POSITION_WORD[sp.state] && (!sp.status || sp.status === 'ok');
    if (!sigs.length && !sp && !sh.error) return null;

    function stateChip(s) {
      if (!(s.state === 0 || s.state === 1)) return null;
      return h('span', { 'class': 'shadow__state', title: 'Двоичный сигнал: включён или выключен' }, [
        h('span', { 'class': 'shadow__dot' + (s.state === 1 ? ' shadow__dot--on' : '') }),
        h('span', { text: s.state === 1 ? 'включён' : 'выключен' })
      ]);
    }
    var cards = sigs.filter(function (s) { return s && typeof s === 'object'; }).map(function (s) {
      var body = [];
      var bad = s.status && s.status !== 'ok';
      if (bad) {
        body.push(h('div', { 'class': 'tile__headline', text: SHADOW_STATUS[s.status] || String(s.status) }));
      } else if (isNum(s.value)) {
        // Единица приходит с сигналом: z, п.п., доля, % — без неё «+0,10» у
        // репрайсинга и «+0,87» у теневой ноги читались бы как одно и то же.
        var unit = s.unit === 'z' ? ' z' : (s.unit === 'доля' ? '' : (s.unit ? ' ' + s.unit : ''));
        body.push(h('div', { 'class': 'tile__num', text: (s.unit === 'доля' ? fmtNum(s.value * 100, 0, false) + '%' : fmtNum(s.value, 2, true) + unit) }));
      } else {
        body.push(h('div', { 'class': 'tile__headline', text: 'нет данных' }));
      }
      if (s.state_month_end === 0 || s.state_month_end === 1) {
        body.push(h('div', { 'class': 'tile__sub', text: 'на конце месяца: ' + (s.state_month_end === 1 ? 'включён' : 'выключен') + ' — так бит читается в теневой позиции' }));
      }
      if (s.note) body.push(h('p', { 'class': 'tile__sub', text: ruText(s.note) }));
      if (Array.isArray(s.history) && s.history.length > 1) {
        body.push(C.miniSeries(s.history, { digits: 2, zero: true, label: 'значение', color: tok('--ink-3'),
          aria: 'История теневого сигнала «' + (s.label || s.id) + '»' }));
      }
      return h('article', { 'class': 'card tile tile--shadow' }, [
        h('div', { 'class': 'tile__head' }, [
          h('h3', { 'class': 'tile__title', text: s.label || s.id }),
          h('span', { 'class': 'tile__head-r' }, [stateChip(s)])
        ])
      ].concat(body, [
        h('div', { 'class': 'tile__foot' }, [
          h('span', { text: s.asof ? 'данные: ' + fmtDay(s.asof) : 'нет данных' }),
          s.group ? h('span', { text: SHADOW_GROUP[s.group] || String(s.group) }) : null
        ])
      ]));
    });
    if (spOk) {
      var spWhy = sp.reason_text || REASON_WORD[sp.reason] || '';
      cards.unshift(h('article', { 'class': 'card tile tile--shadow' }, [
        h('div', { 'class': 'tile__head' }, [h('h3', { 'class': 'tile__title', text: 'Если бы бит репрайсинга был в решении' })]),
        h('div', { 'class': 'tile__num', text: POSITION_WORD[sp.state] }),
        h('div', { 'class': 'tile__sub', text: (sp.since ? 'с ' + fmtDay(sp.since) : '') + (spWhy ? (sp.since ? ' · ' : '') + ruText(spWhy) : '') }),
        (sp.differs_from_main === true || sp.differs_from_main === false) ? h('div', { 'class': 'tile__sub', text:
          (sp.differs_from_main ? 'расходится с позицией в шапке' : 'совпадает с позицией в шапке') +
          (isNum(sp.diff_days_5y) ? '; за пять лет расходились ' + sp.diff_days_5y + ' дн.' : '') +
          (isNum(sp.switches_per_year) ? '; смен в год ' + fmtNum(sp.switches_per_year, 1, false) : '') }) : null,
        h('p', { 'class': 'tile__sub', text: ruText(sp.note || 'Теневая позиция по правилу «пакет + бит репрайсинга ожиданий, читаемый на конце месяца». В решение не входит.') }),
        Array.isArray(sp.history) && sp.history.length ? C.positionRibbon(sp.history, { height: 44, end: sh.asof || d.asof_trading_day, aria: 'Теневая позиция с 2004 года' }) : null,
        h('div', { 'class': 'tile__foot' }, [h('span', { text: 'тень · наблюдение 12 месяцев' })])
      ]));
    } else if (sp) {
      cards.unshift(h('article', { 'class': 'card tile tile--shadow' }, [
        h('div', { 'class': 'tile__head' }, [h('h3', { 'class': 'tile__title', text: 'Если бы бит репрайсинга был в решении' })]),
        h('div', { 'class': 'tile__headline', text: 'теневая позиция ' + (SHADOW_STATUS[sp.status] || 'не посчиталась') + (sp.reason ? ': ' + ruText(sp.reason) : '') }),
        h('div', { 'class': 'tile__foot' }, [h('span', { text: 'тень' })])
      ]));
    }
    return section('Тень', 'считается, копит историю, на позицию не влияет', [
      sh.note ? h('p', { 'class': 'section__note', text: ruText(sh.note) }) : null,
      sh.error ? h('p', { 'class': 'empty', text: 'Тень не посчиталась: ' + String(sh.error) }) : null,
      cards.length ? h('div', { 'class': 'tiles' }, cards) : null
    ]);
  }

  /* ──────────────────────────────────────────────────────── журнал */

  function renderEvents(d) {
    var evs = d.events || [];
    if (!evs.length) return null;
    // Конвейер копит ленту дописыванием в хвост (alerts.py: feed.append), поэтому
    // в витрине она идёт по возрастанию времени — и журнал показывал недельной
    // давности запись сверху, а сегодняшнюю смену знака внизу. Все остальные
    // списки панели идут от свежих к старым; сортируем здесь, а не полагаемся на
    // порядок источника, заодно slice(0,40) начинает резать старое, а не свежее.
    var ordered = evs.slice().sort(function (a, b) { return String(b.ts).localeCompare(String(a.ts)); });
    // Отказы обвязки («источник отдаёт 503») из ленты убраны и уходят в общий
    // телеграм-канал панелей: журнал читают как ленту рынка, и вперемешку с
    // санитарными записями она перестаёт читаться вовсе (alerts.py: OPS_KINDS).
    return section('Журнал', 'события рынка и переходы состояний', [
      h('article', { 'class': 'card' }, ordered.slice(0, 40).map(function (e) {
        var body = [h('span', { 'class': 'evt__t', text: ruText(e.text) })];
        if (e.comment) body.push(h('p', { 'class': 'evt__c', text: ruText(e.comment) }));
        return h('div', { 'class': 'evt' }, [
          // МСК и ДД.ММ, как во всей панели: сырой UTC относил событие закрывающего
          // такта (00:00 МСК = 21:00 UTC) ко вчерашнему дню, а «08-11» читалось
          // как 8 ноября.
          h('span', { 'class': 'evt__ts', text: evtStamp(e.ts) }),
          h('div', { 'class': 'evt__body' }, body)
        ]);
      }))
    ]);
  }

  /* ───────────────────────────────────────────── таблица (доступность) */

  function renderTable(d) {
    var core = d.core || {}, st = d.states || {}, v = d.verdict || {};
    var p = v.position && POSITION_WORD[v.position.state] ? v.position : null;
    var rows = [];
    var series = core.series || [];
    var stMap = {}, gateMap = {};
    (st.series || []).forEach(function (r) { stMap[r[0]] = r[1]; });
    (st.series_gate || []).forEach(function (r) { gateMap[r[0]] = r[1]; });
    var hasGate = Array.isArray(st.series_gate) && st.series_gate.length > 0;
    var hist = p && Array.isArray(p.history) ? p.history : [];
    // Позиция на дату — последний RLE-отрезок, начавшийся не позже этой даты:
    // конвейер отдаёт только смены, а таблица помесячная.
    function posAt(day) {
      var out = null;
      for (var k = 0; k < hist.length; k++) {
        if (String(hist[k][0]) <= day) out = +hist[k][1]; else break;
      }
      return out === 1 ? 'акции' : (out === 0 ? 'деньги' : '—');
    }
    for (var i = Math.max(0, series.length - 36); i < series.length; i++) {
      var day = series[i][0];
      var r = [day, series[i][1], stMap[day] || '—'];
      if (hasGate) r.push(gateMap[day] || '—');
      if (hist.length) r.push(posAt(day));
      rows.push(r);
    }
    rows.reverse();
    var head = [
      h('th', { scope: 'col', text: 'Месяц' }),
      h('th', { scope: 'col', text: 'Композит' }),
      h('th', { scope: 'col', text: 'Ячейка' }),
      hasGate ? h('th', { scope: 'col', text: 'Ворота (с гистерезисом)' }) : null,
      hist.length ? h('th', { scope: 'col', text: 'Позиция' }) : null
    ];
    var table = h('table', { 'class': 'data' }, [
      h('caption', { text: 'Композит ядра, ячейка состояния' + (hasGate ? ', ворота с гистерезисом' : '') +
        (hist.length ? ' и позиция' : '') + ' помесячно, последние 36 месяцев. Полная история — в history/daily.json.' }),
      h('thead', null, [h('tr', null, head)]),
      h('tbody', null, rows.map(function (r) {
        return h('tr', null, [
          h('th', { scope: 'row', text: fmtMon(r[0]) }),
          h('td', { text: fmtNum(+r[1], 2, true) }),
          h('td', { text: r[2] === '—' ? '—' : cellWords(r[2]) })
        ].concat(r.slice(3).map(function (x) { return h('td', { text: x === '—' ? '—' : cellWords(x) }); })));
      }))
    ]);
    var comps = (core.components || []).map(function (c) {
      return h('tr', null, [
        h('th', { scope: 'row', text: c.label || c.id }),
        h('td', { text: fmtNum(isNum(c.z) ? c.z * (c.sign || 1) : null, 2, true) }),
        h('td', { text: (isNum(c.z) ? 'z ' + fmtNum(c.z, 2, true) + ' · ' : '') + (ruText(c.raw_fmt) || '—') }),
        h('td', { text: c.tier || '—' })
      ]);
    });
    var compTable = h('table', { 'class': 'data' }, [
      h('caption', { text: 'Компоненты ядра на текущую дату.' }),
      h('thead', null, [h('tr', null, [
        h('th', { scope: 'col', text: 'Компонент' }), h('th', { scope: 'col', text: 'Вклад' }),
        h('th', { scope: 'col', text: 'Значение' }), h('th', { scope: 'col', text: 'Тир' })
      ])]),
      h('tbody', null, comps)
    ]);

    // Позиция и ворота — то же, что в шапке, но словами и числами в строках:
    // без цвета, без иконок, для проверки.
    var posRows = [];
    if (p) {
      posRows.push(['Позиция', POSITION_WORD[p.state] + (p.since ? ' с ' + fmtDay(p.since) : ''),
        ruText(p.reason_text || REASON_WORD[p.reason] || '')]);
      var prev = v.position_prev_rule;
      if (prev && POSITION_WORD[prev.state]) {
        posRows.push(['Позиция по прежнему правилу', POSITION_WORD[prev.state] + (prev.since ? ' с ' + fmtDay(prev.since) : ''),
          'знак закрытого месяца, ворота без гистерезиса']);
      }
      if (isNum(p.comp_state)) {
        posRows.push(['Знак решения', COMP_STATE_WORD[String(p.comp_state)] || '—',
          'порог ±' + fmtNum(isNum(p.comp_threshold) ? p.comp_threshold : 0.2, 1, false) +
          (isNum(p.comp_daily) ? '; дневной наклон ' + fmtNum(p.comp_daily, 2, true) : '') +
          (p.decision_day ? ' на ' + fmtDay(p.decision_day) : '')]);
      }
      if (p.next_decision) posRows.push(['Следующее решение по наклону', fmtDay(p.next_decision), weekdayOf(p.next_decision)]);
      if (isNum(p.cash_rate)) posRows.push(['Ставка денег', fmtNum(p.cash_rate, 2, false) + '% годовых',
        'вклады топ-10' + (p.cash_rate_asof ? ' на ' + fmtDay(p.cash_rate_asof) : '')]);
      if (isNum(p.switches_per_year)) posRows.push(['Смен позиции в год', fmtNum(p.switches_per_year, 1, false), 'в среднем за пять лет']);
    }
    var gate = st.gate && typeof st.gate === 'object' ? st.gate : null;
    if (gate) {
      posRows.push(['Ворота', gate.open === true ? 'открыты' : (gate.open === false ? 'закрыты' : '—'),
        ((v.regime || st.regime || {}).label || '') + (gate.cell_code ? ' · ' + cellWords(gate.cell_code) : '')]);
      ['trend', 'vol', 'bond'].forEach(function (k) {
        var b = gate[k];
        if (!(b === 0 || b === 1 || b === true || b === false)) return;
        var on = b === 1 || b === true;
        posRows.push([BIT_LABEL[k].k + ' (с гистерезисом)', on ? BIT_LABEL[k].on : BIT_LABEL[k].off,
          (gate.since || {})[k] ? 'с ' + fmtDay(gate.since[k]) : '']);
      });
    }
    var posTable = posRows.length ? h('table', { 'class': 'data' }, [
      h('caption', { text: 'Позиция и ворота на текущую дату.' }),
      h('thead', null, [h('tr', null, [
        h('th', { scope: 'col', text: 'Что' }), h('th', { scope: 'col', text: 'Значение' }), h('th', { scope: 'col', text: 'Пояснение' })
      ])]),
      h('tbody', null, posRows.map(function (r) {
        return h('tr', null, [h('th', { scope: 'row', text: r[0] }), h('td', { text: r[1] }), h('td', { text: r[2] || '' })]);
      }))
    ]) : null;

    return section('Таблица', 'те же ряды числами — для чтения без цвета и для проверки', [
      h('article', { 'class': 'card' }, [
        posTable ? h('div', { 'class': 'tablewrap' }, [posTable]) : null,
        h('div', { 'class': 'tablewrap', style: posTable ? 'margin-top:20px' : null }, [compTable]),
        h('div', { 'class': 'tablewrap', style: 'margin-top:20px' }, [table])
      ])
    ]);
  }

  /* ────────────────────────────────────────────────────── страница */

  function ageMinutes(iso) {
    if (!iso) return null;
    var t = Date.parse(iso);
    if (!isFinite(t)) return null;
    return (Date.now() - t) / 60000;
  }

  /** Минуты словами «2 ч 40 мин» — одинаково для возраста витрины и для нормы. */
  function hhmm(minutes) {
    if (!isNum(minutes)) return '—';
    var hrs = Math.floor(minutes / 60), mins = Math.round(minutes % 60);
    if (mins === 60) { hrs += 1; mins = 0; }
    if (!hrs) return mins + ' мин';
    return hrs + ' ч' + (mins ? ' ' + mins + ' мин' : '');
  }

  var MODE_WORD = { daily: 'ежедневный прогон', intraday: 'прогон внутри дня', monthly: 'месячный прогон' };

  /** Дата и время публикации по-московски: панель русская, а метка была машинной. */
  // Метка события журнала: «11.08 19:05» по Москве. Тот же сдвиг UTC+3, что в
  // fmtStamp, — подпись обязана совпадать со временем прогонов, а не с часами
  // читателя.
  function evtStamp(iso) {
    var t = Date.parse(iso);
    if (!isFinite(t)) return String(iso || '');
    var msk = new Date(t + 3 * 3600 * 1000);
    function p2(n) { return (n < 10 ? '0' : '') + n; }
    return p2(msk.getUTCDate()) + '.' + p2(msk.getUTCMonth() + 1) + ' ' +
      p2(msk.getUTCHours()) + ':' + p2(msk.getUTCMinutes());
  }

  function fmtStamp(iso) {
    var t = Date.parse(iso);
    if (!isFinite(t)) return String(iso || '—');
    // МСК фиксирован (UTC+3) круглый год — сдвигаем и читаем как UTC, чтобы не
    // зависеть от часового пояса читателя: подпись обязана совпадать с временем
    // прогонов на VPS, а не с настройками ноутбука.
    var msk = new Date(t + 3 * 3600 * 1000);
    function p2(n) { return (n < 10 ? '0' : '') + n; }
    return p2(msk.getUTCDate()) + '.' + p2(msk.getUTCMonth() + 1) + '.' + msk.getUTCFullYear() +
      ', ' + p2(msk.getUTCHours()) + ':' + p2(msk.getUTCMinutes()) + ' МСК';
  }

  /** Опоздала ли публикация против СВОЕГО расписания.
   *  -> {late: bool, byMin: число, dueAt: Date|null} либо null, если судить не по чему.
   *
   *  Конвейер молчит по расписанию десять часов в сутки: последний такт 21:00 UTC
   *  (00:00 МСК), первый следующий — 07:00 (10:00 МСК). Плоская норма в 150 минут
   *  превращала эту тишину в ежедневную семичасовую тревогу «данные устарели» —
   *  при том что числа были свежайшие из возможных: биржа закрыта, меняться нечему.
   *  Тревога, горящая треть суток, перестаёт что-либо значить.
   *
   *  Поэтому сравниваем не с возрастом, а с обещанием витрины (`next_publish_at`):
   *  мёртвый конвейер нового обещания не выпустит, и старое протухнет само.
   *  Без обещания (старая витрина, нечитаемое расписание) — прежняя плоская норма. */
  function publishLateness(d) {
    var due = Date.parse(d.next_publish_at || '');
    var grace = isNum(d.stale_grace_minutes) ? d.stale_grace_minutes : 30;
    if (isFinite(due)) {
      var byMin = (Date.now() - due) / 60000;
      return { late: byMin > grace, byMin: byMin, dueAt: new Date(due) };
    }
    var age = ageMinutes(d.generated_at);
    var limit = isNum(d.stale_after_minutes) ? d.stale_after_minutes : 150;
    if (!isNum(age)) return null;
    return { late: age > limit, byMin: age - limit, dueAt: null };
  }

  function renderBanners(d) {
    var box = document.getElementById('banners');
    box.innerHTML = '';
    var age = ageMinutes(d.generated_at);
    var limit = isNum(d.stale_after_minutes) ? d.stale_after_minutes : 150;
    var late = publishLateness(d);
    if (late && late.late) {
      // Норму печатаем в той же форме, что и возраст. Округление нормы до часов
      // делало баннер самоопровергающимся: при норме 150 минут он сообщал «при
      // норме 3 ч», и весь диапазон 150–180 минут тревога висела при возрасте
      // МЕНЬШЕ заявленной нормы — читатель решал, что сломана панель, а не данные.
      box.appendChild(h('div', { 'class': 'banner banner--warn' }, [
        ico('warn', 'banner__ico'),
        h('div', null, [
          h('b', { text: 'Данные устарели. ' }),
          h('span', { text: late.dueAt
            ? ('Очередная публикация ждалась в ' + fmtStamp(late.dueAt.toISOString()) +
               ', её нет уже ' + hhmm(late.byMin) + '. Последняя была ' + hhmm(age) +
               ' назад — числа на экране последние успешные, а не сегодняшние.')
            : ('Публикация была ' + hhmm(age) + ' назад при норме ' + hhmm(limit) +
               '. Числа на экране — последние успешные, а не сегодняшние.') })
        ])
      ]));
    }
    /* Две разные беды, и называть их одним словом нельзя. Источник может
       МОЛЧАТЬ (не ответил, не опрошен) — а может отвечать исправно и отдавать
       СТАРОЕ. Второе неделю выглядело работой: FRED каждый день возвращал Brent
       недельной давности, ALGOPACK падал по сертификату, futoi жил бесплатным
       потоком с задержкой в две недели. Поэтому у второй строки в тексте стоит
       ДАТА ДАННЫХ — то, чего читателю и не хватало, чтобы заметить. */
    var mute = [], old_ = [];
    Object.keys(d.sources || {}).forEach(function (k) {
      var s = d.sources[k];
      if (!s) return;
      // manual_needed — «живой источник молчит, взят ручной резерв»: для
      // читателя это ровно то же, что stale, и молчать об этом нельзя.
      if (s.status === 'error' || s.status === 'manual_needed' ||
          (s.status === 'stale' && s.stale_reason !== 'data')) mute.push(k);
      else if (s.status === 'stale') old_.push(k + ' (данные от ' + fmtDay(s.asof) + ')');
    });
    if (mute.length) {
      box.appendChild(h('div', { 'class': 'banner' }, [
        ico('info', 'banner__ico'),
        h('div', null, [
          h('b', { text: 'Часть источников молчит: ' }),
          h('span', { text: mute.join(', ') + '. Затронутые тайлы помечены жёлтой точкой; ядро и состояния считаются по последним доступным данным.' })
        ])
      ]));
    }
    if (old_.length) {
      box.appendChild(h('div', { 'class': 'banner' }, [
        ico('info', 'banner__ico'),
        h('div', null, [
          h('b', { text: 'Источник отвечает, но обновляться перестал: ' }),
          h('span', { text: old_.join(', ') + '. Панель показывает последнее известное значение как сегодняшнее — оно тянется вперёд, пока не выйдет лимит, и только потом сигнал исчезнет.' })
        ])
      ]));
    }
  }

  /** Шапка и подвал: их обновляем каждую минуту даже когда витрина не менялась —
   *  возраст публикации идёт по часам читателя, а не по данным. */
  function paintMeta(d) {
    var asof = document.getElementById('asof');
    asof.textContent = 'по ' + fmtDay(d.asof_trading_day) + (d.run_mode === 'intraday' ? ' · внутри дня' : '');
    var age = ageMinutes(d.generated_at);
    var ageEl = document.getElementById('age');
    var limit = isNum(d.stale_after_minutes) ? d.stale_after_minutes : 150;
    if (isNum(age)) {
      // На узком экране префикс «обновлено» съедает место, которого не хватает
      // кнопкам: смысл несёт число, а не слово.
      var terse = window.innerWidth < 620;
      var body = age < 1 ? 'только что'
        : (age < 60 ? Math.round(age) + ' мин назад' : Math.floor(age / 60) + ' ч назад');
      ageEl.textContent = terse ? body : 'обновлено ' + body;
      // Подсказка называет и то, когда ждём следующую: без неё «обновлено 5 ч назад»
      // ночью выглядит поломкой, хотя это расписание.
      var late = publishLateness(d);
      ageEl.title = 'Публикация витрины: ' + fmtStamp(d.generated_at) +
        (late && late.dueAt ? '. Следующая ждётся ' + fmtStamp(late.dueAt.toISOString()) : '');
      ageEl.className = 'age' + (late && late.late ? ' age--stale' : '');
    }
    var meta = document.getElementById('foot-meta');
    // Машинная метка «2026-08-11T14:04:42Z» среди дат вида 11.08.2026 читалась как
    // недоделка, а UTC против МСК давал читателю необъяснённые три часа разницы.
    // Номер схемы человеку не говорит ничего — прячем в подсказку.
    meta.textContent = 'Данные конвейера от ' + fmtStamp(d.generated_at) +
      ' · ' + (MODE_WORD[d.run_mode] || d.run_mode || '—') +
      '. Методика и проверка гипотез — validation/VALIDATION.md и validation/REGIME.md в репозитории.';
    meta.title = 'Схема витрины ' + (d.schema || '—') + '; метка публикации ' + (d.generated_at || '—');
  }

  /** Какой из графиков сейчас держит фокус клавиатуры (индекс среди фигур #app). */
  function focusedFigure() {
    var a = document.activeElement;
    if (!a || a.tagName.toLowerCase() !== 'svg') return -1;
    var figs = document.querySelectorAll('#app svg.fig[tabindex]');
    for (var i = 0; i < figs.length; i++) if (figs[i] === a) return i;
    return -1;
  }
  function refocusFigure(i) {
    if (i < 0) return;
    var figs = document.querySelectorAll('#app svg.fig[tabindex]');
    if (figs[i] && document.activeElement !== figs[i]) figs[i].focus();
  }

  function render(d, force) {
    // Полная пересборка #app уносит фокус клавиатуры в начало таб-порядка: узел,
    // на котором стояли стрелками, физически удаляется. Раз в минуту это делало
    // чтение графика с клавиатуры невозможным — а чаще всего пересобирать было
    // и нечего: витрина публикуется раз в пять минут, а панель тянет её раз в минуту.
    // Поэтому при неизменившемся generated_at (и том же режиме таблицы) обновляем
    // только шапку с возрастом и баннеры, а DOM не трогаем вовсе.
    var tableOn = document.getElementById('table-toggle').getAttribute('aria-pressed') === 'true';
    // Без метки публикации сравнивать нечего — тогда перерисовываем всегда, иначе
    // витрина без generated_at застыла бы на первом рендере навсегда.
    var sig = d.generated_at ? String(d.generated_at) + '|' + tableOn : null;
    if (!force && sig && window.__lastPayload && sig === window.__renderSig) {
      window.__lastPayload = d;
      renderBanners(d);
      paintMeta(d);
      return;
    }
    window.__lastPayload = d;
    window.__renderSig = sig;
    C.resetResize();
    var app = document.getElementById('app');
    var scroll = window.scrollY;
    var kbFig = focusedFigure();
    app.innerHTML = '';
    renderBanners(d);
    app.appendChild(renderHero(d));
    app.appendChild(renderCore(d));
    app.appendChild(renderStates(d));
    app.appendChild(renderMonitors(d));
    var shd = renderShadow(d);
    if (shd) app.appendChild(shd);
    var ev = renderEvents(d);
    if (ev) app.appendChild(ev);
    if (tableOn) app.appendChild(renderTable(d));
    // Всё дерево уже в документе — только теперь у контейнеров графиков есть
    // ширина, и их можно рисовать. Второй вызов в следующем кадре добирает те,
    // чей размер на момент первого ещё считался (шрифты, полоса прокрутки).
    C.flush();
    // Фигуры рождаются внутри flush(), поэтому фокус возвращаем только после него
    // (и ещё раз в следующем кадре — там дорисовываются оставшиеся графики).
    refocusFigure(kbFig);
    requestAnimationFrame(function () { C.flush(); refocusFigure(kbFig); });

    paintMeta(d);
    window.scrollTo(0, scroll);
  }

  function showFatal(title, text) {
    document.getElementById('app').innerHTML = '';
    document.getElementById('banners').innerHTML = '';
    document.getElementById('app').appendChild(h('div', { 'class': 'banner banner--err' }, [
      ico('crit', 'banner__ico'),
      h('div', null, [h('b', { text: title + ' ' }), h('span', { text: text })])
    ]));
  }

  function load() {
    fetch(DATA_URL + '?ts=' + Date.now(), { cache: 'no-store' })
      .then(function (r) {
        if (r.status === 503) {
          return r.json().catch(function () { return {}; }).then(function (j) {
            throw new Error('NOTPUB:' + (j.hint || 'конвейер ещё не публиковал витрину'));
          });
        }
        if (!r.ok) throw new Error('HTTP ' + r.status);
        return r.json();
      })
      .then(function (d) { render(d); })
      .catch(function (e) {
        var msg = String(e && e.message || e);
        // Порядок веток важен: 503 проверялся раньше прошлого рендера, и короткий
        // 503 (перезалив бакета, потеря лиза, инцидент R2) стирал исправно
        // отрисованную панель, подменяя её экраном «витрина ещё не публиковалась»
        // у всех открытых вкладок. Прошлые числа с честной отметкой возраста
        // полезнее пустого экрана, а «не публиковалась» — правда только тогда,
        // когда мы ещё ни разу ничего не показали.
        if (window.__lastPayload) {
          var el = document.getElementById('age');
          if (el) { el.textContent = 'связь потеряна'; el.className = 'age age--stale'; }
        } else if (msg.indexOf('NOTPUB:') === 0) {
          showFatal('Витрина ещё не публиковалась.', msg.slice(7));
        } else {
          showFatal('Не удалось загрузить данные.', msg + '. Панель повторит попытку автоматически.');
        }
      });
  }

  function init() {
    initTheme();
    var tt = document.getElementById('table-toggle');
    tt.addEventListener('click', function () {
      var on = tt.getAttribute('aria-pressed') === 'true';
      tt.setAttribute('aria-pressed', on ? 'false' : 'true');
      if (window.__lastPayload) render(window.__lastPayload);
    });
    load();
    setInterval(load, REFRESH_MS);
  }

  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', init);
  else init();
})();
