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

  function currentTheme() {
    // ?theme=light|dark задаёт тему для конкретной ссылки: удобно и для того,
    // чтобы поделиться панелью в нужном виде, и для съёмки страницы роботом.
    var q = (location.search.match(/[?&]theme=(light|dark|auto)/) || [])[1];
    if (q) return q;
    try { return localStorage.getItem('moex-radar-theme') || 'auto'; } catch (e) { return 'auto'; }
  }
  function applyTheme(t) {
    document.documentElement.dataset.theme = (t === 'auto' ? '' : t);
    try { localStorage.setItem('moex-radar-theme', t); } catch (e) { /* приватный режим */ }
    var label = document.getElementById('theme-label');
    if (label) label.textContent = THEME_LABEL[t];
    var btn = document.getElementById('theme-toggle');
    if (btn) btn.setAttribute('title', 'Тема: ' + THEME_LABEL[t] + ' — нажмите, чтобы сменить');
    // Графики читают цвета из CSS-переменных в момент отрисовки, поэтому при смене
    // темы их нужно перерисовать — иначе линии останутся в палитре прежней темы.
    if (window.__lastPayload) render(window.__lastPayload);
  }
  function initTheme() {
    var t = currentTheme();
    applyTheme(t);
    var btn = document.getElementById('theme-toggle');
    if (!btn) return;
    btn.addEventListener('click', function () {
      applyTheme(THEMES[(THEMES.indexOf(currentTheme()) + 1) % THEMES.length]);
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

  function renderHero(d) {
    var v = d.verdict || {}, st = (d.states || {}).current || {}, core = d.core || {};
    var since = st.since || {};
    var bits = ['trend', 'vol', 'bond'].map(function (key) {
      var raw = st[key];
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
      h('div', { 'class': 'kicker', text: 'Состояние рынка' }),
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

    var right = h('div', { 'class': 'hero__gauge' }, [
      h('div', { 'class': 'kicker', text: 'Композит ядра' }),
      h('div', { 'class': 'gaugeval' }, [
        h('span', { 'class': 'gaugeval__n ' + toneOf(core.value), text: fmtNum(core.value, 2, true) }),
        h('span', { 'class': 'gaugeval__l', text: v.core_label || '' })
      ]),
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
        h('span', { text: core.sign_since ? 'знак не менялся с ' + fmtDay(core.sign_since) : 'знак ещё не определялся' })
      ])
    ]);

    var ruleIco = ico('info', 'rule__ico');
    return h('div', { 'class': 'hero' }, [
      h('div', { 'class': 'hero__grid' }, [left, right]),
      v.rule ? h('div', { 'class': 'rule' }, [
        ruleIco,
        h('div', null, [
          h('div', { 'class': 'rule__k', text: 'Правило дня' }),
          h('p', { 'class': 'rule__t', text: v.rule })
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
              h('span', { 'class': 'comp__raw', text: (isNum(c.z) ? 'z ' + fmtNum(c.z, 2, true) + ' · ' : '') + (c.raw_fmt || '') })
            ])
          ])
        ]),
        C.spark(c.spark, tok(i === 0 ? '--s1' : (i === 1 ? '--s2' : '--s3')),
          { aria: 'z-скор «' + (c.label || c.id) + '» за два года' }),
        h('p', { 'class': 'comp__mech', text: c.mechanism || '' }),
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
        h('p', { 'class': 'dist__t', text: x.text || '' })
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
          vIco, h('span', { text: s.verdict || 'нет данных' }),
          isNum(s.value) ? h('span', { 'class': 'tone-mut',
            text: '· z ' + fmtNum(s.z, 2, true) + ', сейчас ' + fmtNum(s.value, 2, true) }) : null
        ]),
        h('p', { 'class': 'sig__why', text: s.why || '' })
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
          out.push(C.flowBars(p.months, fiz, { label: 'физлица, нетто', unit: ' млрд', aria: 'Нетто-покупки акций физлицами' }));
          out.push(h('div', { 'class': 'tile__sub', text: 'Столбики — физлица; управляющие и банки в подсказке ленты.' }));
        }
        break;
      case 'lqdt':
        out.push(num(p.aum, 0, ' млрд ₽'));
        out.push(h('div', { 'class': 'tile__sub', text: p.rotation_started ? 'Ротация началась' : 'Большой ротации ещё не случалось' }));
        break;
      case 'deposit_spread':
        out.push(num(p.spread_pp, 1, ' п.п.', true));
        out.push(h('div', { 'class': 'tile__sub', text: 'Вклады ' + fmtNum(p.deposit_pct, 2, false) + '% против дивидендов ' + fmtNum(p.dy_trail_pct, 1, false) + '%' }));
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
        out.push(h('div', { 'class': 'tile__sub', text: 'z нетто-позиции физлиц за 120 дней; лонгов ' +
          fmtNum(p.holders_long, 0, false) + ', шортов ' + fmtNum(p.holders_short, 0, false) }));
        if (p.series) out.push(C.miniSeries(p.series, { digits: 2, zero: true, label: 'нетто/брутто', color: tok('--s3') }));
        break;
      case 'rub_barrel':
        out.push(num(p.tax_barrel_rub, 0, ' ₽'));
        out.push(h('div', { 'class': 'tile__sub', text: 'налоговая бочка против бюджетных ' +
          fmtNum(p.budget_barrel_rub, 0, false) + ' ₽ — ' + fmtNum(p.gap_pct, 0, true) + '%' }));
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
        out.push(h('div', { 'class': 'tile__sub', text: isNum(p.pct_3y) ? Math.round(p.pct_3y * 100) + '-й перцентиль за три года' : '' }));
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
        out.push(num(p.placed_bn, 1, ' млрд ₽'));
        out.push(h('div', { 'class': 'tile__sub', text: p.failed ? 'аукцион ' + fmtDay(p.date) + ' не состоялся'
          : 'спрос ' + fmtNum(p.demand_bn, 1, false) + ' млрд, bid-to-cover ' + fmtNum(p.bid_to_cover, 2, false) }));
        break;
      case 'sep_node':
        out.push(h('div', { 'class': 'tile__headline', text: m.headline || '' }));
        break;
      default:
        out.push(h('div', { 'class': 'tile__headline', text: m.headline || 'нет данных' }));
    }
    if (!out.length) out.push(h('div', { 'class': 'tile__headline', text: m.headline || 'нет данных' }));
    return out;
  }

  function renderMonitors(d) {
    var tiles = (d.monitors || []).map(function (m) {
      var body = tileBody(m);
      return h('article', { 'class': 'card tile' + (m.tier === 'dead' ? ' tile--dead' : '') }, [
        h('div', { 'class': 'tile__head' }, [
          h('h3', { 'class': 'tile__title', text: m.title || m.id }),
          h('span', { 'class': 'tile__head-r' }, [tier(m.tier), statusDot(m.status)])
        ])
      ].concat(body, [
        trimNote(m.note) ? h('p', { 'class': 'tile__sub', text: trimNote(m.note) }) : null,
        h('div', { 'class': 'tile__foot' }, [
          h('span', { text: m.asof ? 'данные: ' + (String(m.asof).length > 7 ? fmtDay(m.asof) : fmtMon(m.asof + '-01')) : 'данных нет' })
        ])
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
    return section('Журнал', 'события конвейера и переходы состояний', [
      h('article', { 'class': 'card' }, evs.slice(0, 40).map(function (e) {
        return h('div', { 'class': 'evt' }, [
          h('span', { 'class': 'evt__ts', text: (e.ts || '').slice(5, 16).replace('T', ' ') }),
          h('span', { 'class': 'evt__t', text: e.text || '' })
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
        h('td', { text: (isNum(c.z) ? 'z ' + fmtNum(c.z, 2, true) + ' · ' : '') + (c.raw_fmt || '—') }),
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

  function renderBanners(d) {
    var box = document.getElementById('banners');
    box.innerHTML = '';
    var age = ageMinutes(d.generated_at);
    var limit = isNum(d.stale_after_minutes) ? d.stale_after_minutes : 150;
    if (isNum(age) && age > limit) {
      var hrs = Math.floor(age / 60), mins = Math.round(age % 60);
      box.appendChild(h('div', { 'class': 'banner banner--warn' }, [
        ico('warn', 'banner__ico'),
        h('div', null, [
          h('b', { text: 'Данные устарели. ' }),
          h('span', { text: 'Публикация была ' + (hrs ? hrs + ' ч ' : '') + mins + ' мин назад при норме ' +
            Math.round(limit / 60) + ' ч. Числа на экране — последние успешные, а не сегодняшние.' })
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

  function render(d) {
    window.__lastPayload = d;
    C.resetResize();
    var app = document.getElementById('app');
    var scroll = window.scrollY;
    app.innerHTML = '';
    renderBanners(d);
    app.appendChild(renderHero(d));
    app.appendChild(renderCore(d));
    app.appendChild(renderStates(d));
    app.appendChild(renderMonitors(d));
    var ev = renderEvents(d);
    if (ev) app.appendChild(ev);
    if (document.getElementById('table-toggle').getAttribute('aria-pressed') === 'true') {
      app.appendChild(renderTable(d));
    }
    // Всё дерево уже в документе — только теперь у контейнеров графиков есть
    // ширина, и их можно рисовать. Второй вызов в следующем кадре добирает те,
    // чей размер на момент первого ещё считался (шрифты, полоса прокрутки).
    C.flush();
    requestAnimationFrame(function () { C.flush(); });

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
      ageEl.title = 'Публикация витрины: ' + (d.generated_at || '—');
      ageEl.className = 'age' + (age > limit ? ' age--stale' : '');
    }
    var meta = document.getElementById('foot-meta');
    meta.textContent = 'Данные конвейера от ' + (d.generated_at || '—') +
      ' · режим ' + (d.run_mode || '—') + ' · схема ' + (d.schema || '—') +
      '. Методика и проверка гипотез — validation/VALIDATION.md и validation/REGIME.md в репозитории.';
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
      .then(render)
      .catch(function (e) {
        var msg = String(e && e.message || e);
        if (msg.indexOf('NOTPUB:') === 0) {
          showFatal('Витрина ещё не публиковалась.', msg.slice(7));
        } else if (window.__lastPayload) {
          // Держим предыдущий рендер: мигать скелетом на каждом сбое сети хуже,
          // чем показать прежние числа с честной отметкой возраста.
          var el = document.getElementById('age');
          if (el) { el.textContent = 'связь потеряна'; el.className = 'age age--stale'; }
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
