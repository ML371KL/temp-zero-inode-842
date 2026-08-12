/* MOEX Radar — сборка страницы из data.json (docs/CONTRACT.md §3).
 *
 * Ничего не выдумываем: на экран попадает только то, что положил конвейер. Пустое
 * поле рисуется честным «нет данных», а не прочерком, за которым не видно разницы
 * между «источник молчит» и «значение равно нулю».
 *
 * Цвет нигде не работает в одиночку: у состояния ячейки, статуса источника и
 * вердикта сигнала рядом с цветом всегда стоят иконка и словесная подпись — это
 * требование доступности и заодно защита от чтения панели в оттенках серого.
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
    // ?theme=light|dark задаёт тему для конкретной ссылки: удобно и для того,
    // чтобы поделиться панелью в нужном виде, и для съёмки страницы роботом.
    var q = (location.search.match(/[?&]theme=(light|dark|auto)/) || [])[1];
    if (q) return q;
    try { return localStorage.getItem('moex-radar-theme') || 'auto'; } catch (e) { return 'auto'; }
  }
  function applyTheme(t, persist) {
    themeState = t;
    document.documentElement.dataset.theme = (t === 'auto' ? '' : t);
    // В хранилище пишем только собственный выбор посетителя. Тема из адреса —
    // свойство ссылки, а не человека: раньше она затирала сохранённый выбор
    // навсегда, и получатель ссылки терял свою тему на всех вкладках.
    if (persist) { try { localStorage.setItem('moex-radar-theme', t); } catch (e) { /* приватный режим */ } }
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
    var label = { ok: 'свежие данные', stale: 'данные устарели', error: 'источник не ответил', missing: 'данных ещё нет' };
    return h('span', {
      'class': 'dot-status dot-status--' + (status || 'missing'),
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
      return h('span', { 'class': 'bit', title: (x.label || k) + ' на ' + fmtDay(x.asof) }, [
        h('span', { 'class': 'bit__k', text: x.label || k }),
        h('span', { 'class': 'bit__v', text: fmtNum(x.value, QUOTE_DIGITS[k] == null ? 2 : QUOTE_DIGITS[k]) }),
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

  function renderHero(d) {
    var v = d.verdict || {}, st = (d.states || {}).current || {}, core = d.core || {};
    var since = st.since || {};
    var bits = ['trend', 'vol', 'bond'].map(function (key) {
      var raw = st[key];
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
      // Для тренда «единица» — это бык, то есть хорошо; для волы и облигаций
      // единица означает стресс. Цвет ставим по СМЫСЛУ, а не по значению бита.
      var good = key === 'trend' ? on : !on;
      var word = key === 'trend' ? (on ? BIT_LABEL.trend.on : BIT_LABEL.trend.off)
        : (on ? BIT_LABEL[key].on : BIT_LABEL[key].off);
      return h('span', { 'class': 'bit bit--' + (good ? 'off' : 'on') }, [
        h('span', { 'class': 'bit__dot' }),
        h('span', { 'class': 'bit__k', text: BIT_LABEL[key].k }),
        h('span', { 'class': 'bit__v', text: word }),
        since[key] ? h('span', { 'class': 'bit__since', text: 'с ' + fmtDay(since[key]) }) : null
      ]);
    });
    bits.push(h('span', { 'class': 'bit bit--neutral' }, [
      h('span', { 'class': 'bit__dot' }),
      h('span', { 'class': 'bit__k', text: 'Ставка' }),
      h('span', { 'class': 'bit__v', text: PHASE[String(st.rate_phase)] || 'нет данных' })
    ]));

    var mean = (v.cell_stats || {}).mean_fwd1m_pct;
    var quality = !isNum(mean) ? 'flat' : (mean <= -1.5 ? 'crit' : (mean < 0.3 ? 'warn' : 'good'));
    var qColor = { crit: 'var(--crit)', warn: 'var(--warn)', good: 'var(--good)', flat: 'var(--ink-3)' }[quality];

    var cellIco = ico(quality);
    cellIco.setAttribute('class', 'cellname__ico');
    cellIco.style.color = qColor;

    var left = h('div', { 'class': 'hero__cell' }, [
      h('div', { 'class': 'kicker', text: 'Ворота риска · что сейчас можно' }),
      h('div', { 'class': 'bits' }, bits),
      h('div', { 'class': 'cellname' }, [cellIco, h('span', { text: v.cell_label || 'ячейка не определена' })]),
      h('div', { 'class': 'cellcode', text: (v.cell_code || '').split('|').join(' · ') }),
      h('div', { 'class': 'stats' }, [
        stat('Средний форвардный месяц', isNum(mean) ? fmtNum(mean, 2, true) + '%' : '—',
          'на истории 2004–2026', toneOf(mean)),
        stat('Доля плюсовых', isNum((v.cell_stats || {}).hit) ? Math.round(v.cell_stats.hit * 100) + '%' : '—'),
        stat('Наблюдений', isNum((v.cell_stats || {}).n) ? String(v.cell_stats.n) : '—', 'месяцев в ячейке')
      ])
    ]);

    /* Два крупных ответа в шапке спорили друг с другом: «токсичная ячейка» (плохо)
     * и цветное «+0,69 умеренный лонг» (хорошо), причём второе было крупнее и
     * единственным цветным — читатель за три секунды уносил именно его. У чисел
     * разные роли, и их надо назвать: ячейка — ВОРОТА (что можно делать), композит
     * — НАКЛОН (куда). Кегль композита придавливаем здесь же, рядом с ролью:
     * ворота главнее наклона, и на узком экране это должно остаться верным.
     */
    var gate = quality === 'crit' ? 'closed' : (quality === 'warn' ? 'ajar' : (quality === 'good' ? 'open' : null));
    var tilt = isNum(core.value) && core.value !== 0 ? (core.value > 0 ? 'up' : 'down') : null;
    var gateNote = null;
    if (gate && tilt) {
      if (gate === 'closed') {
        gateNote = tilt === 'up'
          ? 'Сигнал есть, но ворота закрыты: ячейка исторически убыточна — вход только по подтверждению.'
          : 'Ворота закрыты и наклон вниз: оба ответа против риска.';
      } else if (gate === 'ajar') {
        gateNote = tilt === 'up'
          ? 'Ворота приоткрыты: наклон вверх, но ячейка около нуля — риск дозируем.'
          : 'Ворота приоткрыты, наклон вниз: добавлять риск не за что.';
      } else {
        gateNote = tilt === 'up'
          ? 'Ворота открыты и наклон вверх: оба ответа в одну сторону.'
          : 'Ворота открыты, но наклон вниз: ячейка разрешает риск, модель его не подтверждает.';
      }
    }
    var me = core.month_end || {};

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
        h('div', { text: core.sign_since ? 'знак не менялся с ' + fmtDay(core.sign_since) : 'знак ещё не определялся' }),
        // Дневное число дрожит внутри месяца, а решение по исследованию — месячное
        // (REGIME §6). Показываем последний ЗАКРЫТЫЙ месяц: именно с ним надо
        // сравнивать «сегодня», иначе месячный шаг живёт только в документации.
        isNum(me.value) ? h('div', {
          title: 'Ребаланс мышления месячный: внутримесячные колебания композита решения не меняют',
          text: 'последний закрытый месяц: ' + fmtNum(me.value, 2, true) +
            (me.label ? ' (' + me.label + ')' : '') + (me.date ? ' на ' + fmtDay(me.date) : '')
        }) : null
      ])
    ]);

    var ruleIco = ico('info', 'rule__ico');
    return h('div', { 'class': 'hero' }, [
      renderQuotes(d),
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
    var hlStatus = { ok: 'работает', warn: 'слабеет', dead: 'сломана' }[hl.status] || 'нет данных';
    var hlIco = ico(hl.status === 'ok' ? 'good' : (hl.status === 'warn' ? 'warn' : (hl.status === 'dead' ? 'crit' : 'flat')));
    hlIco.setAttribute('class', 'sig__ico');
    hlIco.style.color = hl.status === 'ok' ? 'var(--good)' : (hl.status === 'warn' ? 'var(--warn)' : (hl.status === 'dead' ? 'var(--crit)' : 'var(--ink-3)'));

    var health = h('article', { 'class': 'card' }, [
      h('div', { 'class': 'card__head' }, [
        h('h3', { 'class': 'card__title', text: 'Здоровье модели' }),
        h('span', { 'class': 'card__note', text: 'скользящий ранговый IC за 24 месяца' })
      ]),
      h('div', { 'class': 'health' }, [
        stat('IC 24 мес', fmtNum(hl.ic_24m, 2, true), null, toneOf(hl.ic_24m)),
        stat('Наблюдений', isNum(hl.n) ? String(hl.n) : '—'),
        h('div', null, [
          h('div', { 'class': 'stat__k', text: 'Статус' }),
          h('div', { 'class': 'sig__verdict', style: 'margin-top:2px' }, [hlIco, h('span', { text: hlStatus })])
        ])
      ]),
      hl.series ? C.miniSeries(hl.series, {
        height: 44, zero: true, digits: 2, label: 'IC',
        color: tok('--ink-3'), aria: 'Скользящий IC модели по месяцам'
      }) : null,
      h('p', { 'class': 'card__foot', text: hl.status === 'dead'
        ? 'IC ушёл ниже нуля: это повод к ревизии состава ядра, а не к подгонке весов.'
        : (hl.status === 'warn'
          ? 'IC около нуля: модель слабеет. Состав меняют только по итогам реколибровки — не по скользящему IC.'
          : 'Состав ядра фиксирован: отбор по скользящей результативности проверялся на истории и проиграл.') })
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
        h('span', { 'class': 'card__note', text: 'цвет — средняя форвардная доходность ячейки; ▾ — сейчас' })
      ]),
      C.stateRibbon(st.series, cells),
      legend
    ]);

    var dists = (st.distances || []).map(function (x) {
      return h('div', { 'class': 'dist' }, [
        h('div', { 'class': 'dist__row' }, [
          h('span', { 'class': 'dist__k', text: x.label || x.id }),
          h('span', { 'class': 'dist__v', text: fmtNum(x.value, 1, true) + ' → ' + fmtNum(x.threshold, 1, true) })
        ]),
        C.thresholdBar(x.value, x.threshold, { invert: x.id === 'bond' }),
        h('p', { 'class': 'dist__t', text: ruText(x.text) })
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
          isNum(s.value) ? h('span', { 'class': 'tone-mut',
            text: '· z ' + fmtNum(s.z, 2, true) + ', сейчас ' + fmtNum(s.value, 2, true) }) : null
        ]),
        h('p', { 'class': 'sig__why', text: ruText(s.why) })
      ]);
    });

    return section('Машина состояний', 'Слой 2 · ворота риска: какие сигналы включены в текущей ячейке', [
      ribbon,
      h('div', { 'class': 'two', style: 'margin-top:16px' }, [
        h('article', { 'class': 'card' }, [
          h('div', { 'class': 'card__head' }, [h('h3', { 'class': 'card__title', text: 'Расстояние до переключения' })]),
          dists.length ? h('div', null, dists) : h('p', { 'class': 'empty', text: 'Расстояния ещё не рассчитаны' })
        ]),
        h('article', { 'class': 'card' }, [
          h('div', { 'class': 'card__head' }, [
            h('h3', { 'class': 'card__title', text: 'Активные сигналы второго ряда' }),
            h('span', { 'class': 'card__note', text: 'вердикт считается по z-скору' })
          ]),
          sigs.length ? h('div', null, sigs)
            : h('p', { 'class': 'empty', text: 'В текущей ячейке сигналы второго ряда не включены' })
        ])
      ])
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
        out.push(num(p.days_left, 0, ' дн.'));
        out.push(h('div', { 'class': 'tile__sub', text: 'до заседания ' + fmtDay(p.next_meeting) + '; ключевая ' + fmtNum(p.key_rate, 2, false) + '%' +
          (isNum(p.consensus) ? ', консенсус ' + fmtNum(p.consensus, 2, false) + '%' : ', консенсус не внесён') }));
        break;
      case 'polymarket':
        out.push(num(p.prob_pct, 0, '%'));
        out.push(h('div', { 'class': 'tile__sub', text: (isNum(p.chg_7d_pp) ? fmtNum(p.chg_7d_pp, 1, true) + ' п.п. за неделю. ' : '') + (p.question || '') }));
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
          '; лонгов ' + fmtNum(p.holders_long, 0, false) + ', шортов ' + fmtNum(p.holders_short, 0, false) }));
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
        out.push(h('div', { 'class': 'tile__sub', text: 'недельный принт; SAAR по 4 неделям ' + fmtNum(p.saar_4w_pct, 1, false) + '%' }));
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
    var horizon = d.asof_trading_day || String(d.generated_at || '').slice(0, 10);
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
          h('span', { 'class': 'evt__ts', text: (e.ts || '').slice(5, 16).replace('T', ' ') }),
          h('div', { 'class': 'evt__body' }, body)
        ]);
      }))
    ]);
  }

  /* ───────────────────────────────────────────── таблица (доступность) */

  function renderTable(d) {
    var core = d.core || {}, st = d.states || {};
    var rows = [];
    var series = core.series || [];
    var stMap = {};
    (st.series || []).forEach(function (r) { stMap[r[0]] = r[1]; });
    for (var i = Math.max(0, series.length - 36); i < series.length; i++) {
      rows.push([series[i][0], series[i][1], stMap[series[i][0]] || '—']);
    }
    rows.reverse();
    var table = h('table', { 'class': 'data' }, [
      h('caption', { text: 'Композит ядра и ячейка состояния помесячно, последние 36 месяцев. Полная история — в history/daily.json.' }),
      h('thead', null, [h('tr', null, [
        h('th', { scope: 'col', text: 'Месяц' }),
        h('th', { scope: 'col', text: 'Композит' }),
        h('th', { scope: 'col', text: 'Ячейка' })
      ])]),
      h('tbody', null, rows.map(function (r) {
        return h('tr', null, [
          h('th', { scope: 'row', text: fmtMon(r[0]) }),
          h('td', { text: fmtNum(+r[1], 2, true) }),
          h('td', { text: String(r[2]).split('|').join(' · ') })
        ]);
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
    return section('Таблица', 'те же ряды числами — для чтения без цвета и для проверки', [
      h('article', { 'class': 'card' }, [
        h('div', { 'class': 'tablewrap' }, [compTable]),
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

  function renderBanners(d) {
    var box = document.getElementById('banners');
    box.innerHTML = '';
    var age = ageMinutes(d.generated_at);
    var limit = isNum(d.stale_after_minutes) ? d.stale_after_minutes : 150;
    if (isNum(age) && age > limit) {
      // Норму печатаем в той же форме, что и возраст. Округление нормы до часов
      // делало баннер самоопровергающимся: при норме 150 минут он сообщал «при
      // норме 3 ч», и весь диапазон 150–180 минут тревога висела при возрасте
      // МЕНЬШЕ заявленной нормы — читатель решал, что сломана панель, а не данные.
      box.appendChild(h('div', { 'class': 'banner banner--warn' }, [
        ico('warn', 'banner__ico'),
        h('div', null, [
          h('b', { text: 'Данные устарели. ' }),
          h('span', { text: 'Публикация была ' + hhmm(age) + ' назад при норме ' + hhmm(limit) +
            '. Числа на экране — последние успешные, а не сегодняшние.' })
        ])
      ]));
    }
    var bad = [];
    Object.keys(d.sources || {}).forEach(function (k) {
      var s = d.sources[k];
      if (s && (s.status === 'error' || s.status === 'stale')) bad.push(k);
    });
    if (bad.length) {
      box.appendChild(h('div', { 'class': 'banner' }, [
        ico('info', 'banner__ico'),
        h('div', null, [
          h('b', { text: 'Часть источников молчит: ' }),
          h('span', { text: bad.join(', ') + '. Затронутые тайлы помечены жёлтой точкой; ядро и состояния считаются по последним доступным данным.' })
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
      ageEl.title = 'Публикация витрины: ' + fmtStamp(d.generated_at);
      ageEl.className = 'age' + (age > limit ? ' age--stale' : '');
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
