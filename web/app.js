/* MOEX Radar — фронт панели.
 *
 * Ванильный JS без сборщика и без единого внешнего ресурса: страница обязана работать
 * под строгим CSP (default-src 'self'), поэтому здесь нет ни инлайн-обработчиков в разметке,
 * ни атрибутов style="" — динамические стили ставятся через CSSOM (его CSP не трогает).
 *
 * Разметка строится через createElement/textContent, а не innerHTML: в data.json приходят
 * тексты из внешних источников (заголовки ОРФР, заметки парсеров) — их нельзя вставлять как HTML.
 *
 * Всё, что видно на экране, приходит из /data/data.json (схема — docs/CONTRACT.md §3).
 * Статикой зашиты только подписи осей, пороги шкалы гейджа и расшифровки тиров —
 * это оформление, а не данные.
 */
(function () {
  'use strict';

  // ------------------------------------------------------------- константы
  var DATA_URL = '/data/data.json';
  var REFRESH_MS = 60000;   // автообновление раз в минуту: пайплайн публикует не чаще
  var AGE_TICK_MS = 15000;  // возраст данных пересчитываем локально, без обращения к сети
  var MSK_MS = 3 * 3600 * 1000;

  // Палитра дублирует styles.css (SVG красится атрибутами, а не классами).
  var PAL = {
    fg: '#e6ebf2', muted: '#8b97a8', dim: '#5f6b7d', line: '#242c3a', grid: '#1c2431',
    pos: '#35c07a', neg: '#e5484d', warn: '#f0a03c', info: '#5b9dff', slate: '#46536b'
  };

  // Зеркало TIER_NOTES из pipeline/lib/constants.py. Держать синхронно руками:
  // тянуть словарь в data.json ради четырёх строк — лишние байты в горячем объекте.
  var TIER_NOTES = {
    A: 'Валидировано: значимо на истории и переживает поправки',
    B: 'Направление подтверждено, сила умеренная/режимная',
    monitor: 'Мониторинг: предиктивность не доказана (мало истории или событий)',
    dead: 'Как предиктор акций опровергнуто — контекст, не сигнал'
  };
  var TIER_LABEL = { A: 'тир A', B: 'тир B', monitor: 'монитор', dead: 'опровергнут' };

  // Границы зон гейджа — зеркало CORE_LABELS. Это шкала рисунка; подпись под стрелкой
  // всегда берётся из verdict.core_label, чтобы фронт не спорил с пайплайном.
  var GAUGE_ZONES = [
    { from: -3, to: -1, color: PAL.neg, alpha: 0.75 },
    { from: -1, to: -0.3, color: PAL.neg, alpha: 0.32 },
    { from: -0.3, to: 0.3, color: PAL.slate, alpha: 0.85 },
    { from: 0.3, to: 1, color: PAL.pos, alpha: 0.32 },
    { from: 1, to: 3, color: PAL.pos, alpha: 0.75 }
  ];
  var GAUGE_TICKS = [-3, -1, 0, 1, 3];

  // Оси машины состояний: 1 = «включено». vol=1 не «плохо», а «шип» — красить янтарём,
  // покупаемость шипа определяется облигационным флагом (REGIME.md §2).
  var AXES = [
    { key: 'trend', label: 'Тренд', on: 'бык', off: 'медведь', onTone: 'pos', offTone: 'neg' },
    { key: 'vol', label: 'Волатильность', on: 'стресс', off: 'спокойно', onTone: 'warn', offTone: 'pos' },
    { key: 'bond', label: 'Облигации', on: 'стресс', off: 'ок', onTone: 'neg', offTone: 'pos' }
  ];
  var RATE_PHASE = {
    '-1': { text: 'смягчение', tone: 'pos' },
    '0': { text: 'пауза', tone: 'mut' },
    '1': { text: 'ужесточение', tone: 'neg' }
  };
  var CELL_WORDS = { bull: 'бык', bear: 'медведь', calm: 'спокойно', stress: 'стресс', ok: 'ок' };
  var DIST_LABEL = {
    trend: 'Тренд (MA200)', vol: 'Волатильность', bond: 'Облигационный флаг',
    rate: 'Ставка', rate_phase: 'Фаза ставки'
  };
  var STATUS_LABEL = { ok: 'свежо', stale: 'протухло', missing: 'нет данных', error: 'ошибка источника' };
  var HEALTH_LABEL = { ok: 'в норме', warn: 'слабеет', dead: 'модель мертва' };
  var EVENT_KIND = {
    state_change: 'состояние', core_flip: 'ядро', cb: 'ЦБ', source: 'источник',
    buy_window_open: 'окно входа', lease: 'публикация'
  };
  var MONTHS = ['января', 'февраля', 'марта', 'апреля', 'мая', 'июня',
    'июля', 'августа', 'сентября', 'октября', 'ноября', 'декабря'];
  var MONTHS_NOM = ['январь', 'февраль', 'март', 'апрель', 'май', 'июнь',
    'июль', 'август', 'сентябрь', 'октябрь', 'ноябрь', 'декабрь'];

  var EVENTS_VISIBLE = 25;   // журнал длинный, на телефоне разворачиваем по кнопке

  // --------------------------------------------------------------- формат
  var nfCache = {};
  function nf(digits, signed) {
    var key = digits + (signed ? 's' : '');
    if (!nfCache[key]) {
      nfCache[key] = new Intl.NumberFormat('ru-RU', {
        minimumFractionDigits: digits, maximumFractionDigits: digits,
        signDisplay: signed ? 'exceptZero' : 'auto'
      });
    }
    return nfCache[key];
  }
  function isNum(v) { return typeof v === 'number' && isFinite(v); }
  function num(v, digits, signed) {
    if (!isNum(v)) return '—';
    // Intl отдаёт дефис-минус, а тексты пайплайна (правило дня, заголовки тайлов) написаны
    // типографским минусом. Разнобой в одной строке заметен — приводим к U+2212.
    return nf(digits == null ? 2 : digits, !!signed).format(v).replace(/-/g, '−');
  }
  function pct(v, digits, signed) {
    if (!isNum(v)) return '—';
    return num(v, digits == null ? 2 : digits, signed !== false) + '%';
  }
  // Разрядность «по величине»: мониторы отдают и миллиарды рублей, и доли процента.
  function smart(v) {
    if (!isNum(v)) return '—';
    var a = Math.abs(v);
    return num(v, a >= 1000 ? 0 : a >= 100 ? 1 : a >= 1 ? 2 : 3, false);
  }
  function tone(v) { return !isNum(v) || v === 0 ? '' : (v > 0 ? ' tone--pos' : ' tone--neg'); }

  function pad2(n) { return n < 10 ? '0' + n : String(n); }
  function parseTs(s) {
    if (typeof s !== 'string') return null;
    var t = Date.parse(s);
    return isFinite(t) ? t : null;
  }
  // Внутри всё в UTC, показываем МСК. Сдвигаем метку и читаем UTC-геттерами —
  // так результат не зависит от часового пояса телефона (пользователь бывает в Малайзии).
  function mskParts(ts) {
    var d = new Date(ts + MSK_MS);
    return {
      d: d.getUTCDate(), m: d.getUTCMonth(), y: d.getUTCFullYear(),
      hh: d.getUTCHours(), mm: d.getUTCMinutes()
    };
  }
  function fmtMsk(ts) {
    var p = mskParts(ts);
    return pad2(p.d) + '.' + pad2(p.m + 1) + '.' + p.y + ' ' + pad2(p.hh) + ':' + pad2(p.mm);
  }
  function fmtMskShort(ts) {
    var p = mskParts(ts);
    return pad2(p.d) + '.' + pad2(p.m + 1) + ' ' + pad2(p.hh) + ':' + pad2(p.mm);
  }
  // Календарные строки («2026-08-11», «2026-07») — это даты, а не моменты: часовой сдвиг к ним
  // не применяется, иначе месячный тайл переедет на день назад.
  function fmtDay(s) {
    if (typeof s !== 'string') return '—';
    var m = /^(\d{4})-(\d{2})-(\d{2})/.exec(s);
    if (m) return m[3] + '.' + m[2] + '.' + m[1];
    var mo = /^(\d{4})-(\d{2})$/.exec(s);
    if (mo) return (MONTHS_NOM[Number(mo[2]) - 1] || mo[2]) + ' ' + mo[1];
    return s;
  }
  function fmtDayShort(s) {
    var m = /^(\d{4})-(\d{2})-(\d{2})$/.exec(String(s));
    if (!m) return fmtDay(s);
    return Number(m[3]) + ' ' + (MONTHS[Number(m[2]) - 1] || '') + ' ' + m[1];
  }
  function fmtAge(ms) {
    if (!isFinite(ms)) return '—';
    var min = Math.max(0, Math.round(ms / 60000));
    if (min < 60) return min + ' мин';
    var h = Math.floor(min / 60), rest = min % 60;
    if (h < 48) return h + ' ч ' + rest + ' мин';
    var d = Math.floor(h / 24);
    return d + ' дн ' + (h % 24) + ' ч';
  }

  // ------------------------------------------------------------------ DOM
  function el(tag, attrs, kids) {
    var n = document.createElement(tag), k;
    if (attrs) {
      for (k in attrs) {
        if (!Object.prototype.hasOwnProperty.call(attrs, k)) continue;
        var v = attrs[k];
        if (v == null || v === false) continue;
        if (k === 'class') n.className = v;
        else if (k === 'text') n.textContent = String(v);
        else n.setAttribute(k, v === true ? '' : String(v));
      }
    }
    append(n, kids);
    return n;
  }
  function append(node, kids) {
    if (kids == null || kids === false) return;
    if (Array.isArray(kids)) {
      for (var i = 0; i < kids.length; i++) append(node, kids[i]);
      return;
    }
    node.appendChild(typeof kids === 'object' ? kids : document.createTextNode(String(kids)));
  }
  var NS = 'http://www.w3.org/2000/svg';
  function sv(tag, attrs, kids) {
    var n = document.createElementNS(NS, tag), k;
    if (attrs) {
      for (k in attrs) {
        if (!Object.prototype.hasOwnProperty.call(attrs, k)) continue;
        var v = attrs[k];
        if (v == null || v === false) continue;
        if (k === 'text') { n.textContent = String(v); continue; }
        n.setAttribute(k, String(v));
      }
    }
    if (kids) append(n, kids);
    return n;
  }
  function svgFrame(w, h, cls) {
    // aria-hidden: график всегда сопровождается теми же числами текстом, второй раз
    // озвучивать скринридеру нечего.
    return sv('svg', {
      'class': cls || 'chart', viewBox: '0 0 ' + w + ' ' + h,
      width: '100%', 'aria-hidden': 'true', focusable: 'false'
    });
  }
  function emptyChart(text) { return el('div', { 'class': 'empty empty--chart', text: text }); }
  // Ширина viewBox широких графиков подгоняется под реальную ширину экрана, чтобы масштаб
  // был ~1:1. Иначе на десктопе SVG растягивается и подписи осей раздуваются до 20+ px.
  function fullChartW() {
    var w = window.innerWidth || 360;
    return Math.max(320, Math.min(1040, Math.round(w - 44)));
  }
  function section(title, sub, kids) {
    return el('section', { 'class': 'section' }, [
      el('h2', { 'class': 'section__title', text: title }),
      sub ? el('p', { 'class': 'section__sub', text: sub }) : null,
      kids
    ]);
  }

  // Бейдж тира: расшифровка прячется за нажатием — на телефоне title не показывается,
  // а класть четыре строки под каждый тайл — визуальный шум.
  function tierBadge(tier) {
    var known = Object.prototype.hasOwnProperty.call(TIER_NOTES, tier);
    if (!known) return { btn: el('span', { 'class': 'badge', text: String(tier || '—') }), note: null };
    var note = el('p', { 'class': 'tier-note', text: TIER_NOTES[tier] });
    var btn = el('button', {
      'class': 'badge badge--' + tier, type: 'button', 'aria-expanded': 'false',
      title: TIER_NOTES[tier], text: TIER_LABEL[tier]
    });
    btn.addEventListener('click', function () {
      var open = note.classList.toggle('is-open');
      btn.setAttribute('aria-expanded', open ? 'true' : 'false');
    });
    if (tier === 'dead') { note.classList.add('is-open'); btn.setAttribute('aria-expanded', 'true'); }
    return { btn: btn, note: note };
  }

  // ------------------------------------------------------------ ряды/точки
  function toPts(pairs) {
    var out = [], i, p, v, t, useIndex = false;
    if (!Array.isArray(pairs)) return out;
    for (i = 0; i < pairs.length; i++) {
      p = pairs[i];
      if (!Array.isArray(p) || p.length < 2) continue;
      v = Number(p[1]);
      if (!isFinite(v)) continue;              // пропуск источника рисовать нечем
      t = parseTs(p[0]);
      if (t == null) useIndex = true;
      out.push({ t: t, v: v, d: p[0], i: out.length });
    }
    // Если хоть одна дата не разобралась — переходим на равномерную ось по индексу,
    // иначе смешение шкал даёт кашу.
    if (useIndex) for (i = 0; i < out.length; i++) out[i].t = i;
    else out.sort(function (a, b) { return a.t - b.t; });
    return out;
  }
  // Прореживание для длинных лент (2004+): в каждой корзине оставляем минимум и максимум,
  // поэтому экстремумы и переходы через ноль не исчезают с картинки.
  function decimate(pts, maxN) {
    if (pts.length <= maxN) return pts;
    var step = pts.length / maxN, out = [], i, j, a, b, mn, mx;
    for (i = 0; i < maxN; i++) {
      a = Math.floor(i * step); b = Math.min(pts.length, Math.floor((i + 1) * step));
      if (b <= a) continue;
      mn = pts[a]; mx = pts[a];
      for (j = a; j < b; j++) {
        if (pts[j].v < mn.v) mn = pts[j];
        if (pts[j].v > mx.v) mx = pts[j];
      }
      if (mn === mx) out.push(mn);
      else if (mn.t <= mx.t) { out.push(mn); out.push(mx); }
      else { out.push(mx); out.push(mn); }
    }
    return out;
  }
  function extent(pts) {
    var lo = Infinity, hi = -Infinity, i;
    for (i = 0; i < pts.length; i++) { if (pts[i].v < lo) lo = pts[i].v; if (pts[i].v > hi) hi = pts[i].v; }
    if (!isFinite(lo)) { lo = 0; hi = 1; }
    if (lo === hi) { lo -= 1; hi += 1; }
    return [lo, hi];
  }
  function yearTicks(t0, t1, maxTicks) {
    var y0 = new Date(t0).getUTCFullYear(), y1 = new Date(t1).getUTCFullYear();
    var stepY = Math.max(1, Math.ceil((y1 - y0 + 1) / maxTicks)), out = [], y, t;
    for (y = Math.ceil(y0 / stepY) * stepY; y <= y1; y += stepY) {
      t = Date.UTC(y, 0, 1);
      if (t >= t0 && t <= t1) out.push({ t: t, label: String(y) });
    }
    return out;
  }

  // ------------------------------------------------------------- графики
  // Спарклайн z-скора за 2 года. Ноль подчёркнут: у z-скора он и есть точка отсчёта.
  function sparkline(pairs) {
    var pts = decimate(toPts(pairs), 200);
    if (pts.length < 2) return emptyChart('нет данных за период');
    var W = 220, H = 46, pad = 3;
    var ex = extent(pts), lo = Math.min(ex[0], 0), hi = Math.max(ex[1], 0);
    var t0 = pts[0].t, t1 = pts[pts.length - 1].t;
    var xs = function (t) { return t1 === t0 ? pad : pad + (W - 2 * pad) * (t - t0) / (t1 - t0); };
    var ys = function (v) { return pad + (H - 2 * pad) * (hi - v) / (hi - lo); };
    var svg = svgFrame(W, H, 'chart chart--spark'), i, d = '';
    for (i = 0; i < pts.length; i++) d += (i ? 'L' : 'M') + xs(pts[i].t).toFixed(1) + ' ' + ys(pts[i].v).toFixed(1);
    if (lo < 0 && hi > 0) {
      svg.appendChild(sv('line', {
        x1: 0, x2: W, y1: ys(0), y2: ys(0), stroke: PAL.line, 'stroke-width': 1,
        'stroke-dasharray': '3 3', 'vector-effect': 'non-scaling-stroke'
      }));
    }
    var last = pts[pts.length - 1];
    var col = last.v >= 0 ? PAL.pos : PAL.neg;
    svg.appendChild(sv('path', {
      d: d, fill: 'none', stroke: col, 'stroke-width': 1.6,
      'stroke-linejoin': 'round', 'vector-effect': 'non-scaling-stroke'
    }));
    svg.appendChild(sv('circle', { cx: xs(last.t), cy: ys(last.v), r: 2.4, fill: col }));
    return svg;
  }

  // Лента композита с 2004: линия + заливка по знаку + отметки смен знака.
  function coreRibbon(pairs) {
    var all = toPts(pairs);
    if (all.length < 2) return emptyChart('ещё не публиковалось');
    var W = fullChartW(), H = 118, L = 20, R = 6, T = 8, B = 16;
    var ex = extent(all);
    var span = Math.max(1, Math.ceil(Math.max(Math.abs(ex[0]), Math.abs(ex[1])) * 10) / 10);
    var t0 = all[0].t, t1 = all[all.length - 1].t;
    var xs = function (t) { return t1 === t0 ? L : L + (W - L - R) * (t - t0) / (t1 - t0); };
    var ys = function (v) { return T + (H - T - B) * (span - v) / (2 * span); };
    var svg = svgFrame(W, H), i, g;

    // Сетка: ноль жирнее, ±1 — «сильный сигнал» по договорённости валидации.
    var levels = [span, 1, 0, -1, -span];
    for (i = 0; i < levels.length; i++) {
      var lv = levels[i];
      if (Math.abs(lv) > span) continue;
      svg.appendChild(sv('line', {
        x1: L, x2: W - R, y1: ys(lv), y2: ys(lv),
        stroke: lv === 0 ? PAL.line : PAL.grid, 'stroke-width': lv === 0 ? 1.2 : 1,
        'vector-effect': 'non-scaling-stroke'
      }));
      svg.appendChild(sv('text', {
        x: L - 3, y: ys(lv) + 3, 'text-anchor': 'end', fill: PAL.dim, 'font-size': 8,
        text: (lv > 0 ? '+' : '') + num(lv, Math.abs(lv) % 1 ? 1 : 0)
      }));
    }

    // Смены знака считаем по ПОЛНОМУ ряду (до прореживания), чтобы даты отметок были точными.
    var crosses = [];
    for (i = 1; i < all.length; i++) {
      var a = all[i - 1], b = all[i];
      if ((a.v >= 0) === (b.v >= 0)) continue;
      var f = Math.abs(a.v) / (Math.abs(a.v) + Math.abs(b.v) || 1);
      crosses.push({ t: a.t + (b.t - a.t) * f, to: b.v >= 0 ? 1 : -1, d: b.d });
    }

    var pts = decimate(all, 700);
    // Заливка знаком: режем ряд в точках пересечения нуля и заливаем каждый кусок своим цветом.
    var segs = [], cur = null;
    for (i = 0; i < pts.length; i++) {
      var p = pts[i], s = p.v >= 0 ? 1 : -1;
      if (!cur) { cur = { s: s, pts: [p] }; continue; }
      if (s === cur.s) { cur.pts.push(p); continue; }
      var q = cur.pts[cur.pts.length - 1];
      var fr = Math.abs(q.v) / (Math.abs(q.v) + Math.abs(p.v) || 1);
      var cross = { t: q.t + (p.t - q.t) * fr, v: 0 };
      cur.pts.push(cross); segs.push(cur);
      cur = { s: s, pts: [cross, p] };
    }
    if (cur) segs.push(cur);
    g = sv('g', null);
    for (i = 0; i < segs.length; i++) {
      var sg = segs[i];
      if (sg.pts.length < 2) continue;
      var dd = 'M' + xs(sg.pts[0].t).toFixed(1) + ' ' + ys(0).toFixed(1), j;
      for (j = 0; j < sg.pts.length; j++) dd += 'L' + xs(sg.pts[j].t).toFixed(1) + ' ' + ys(sg.pts[j].v).toFixed(1);
      dd += 'L' + xs(sg.pts[sg.pts.length - 1].t).toFixed(1) + ' ' + ys(0).toFixed(1) + 'Z';
      g.appendChild(sv('path', { d: dd, fill: sg.s > 0 ? PAL.pos : PAL.neg, 'fill-opacity': 0.30 }));
    }
    svg.appendChild(g);

    var d = '';
    for (i = 0; i < pts.length; i++) d += (i ? 'L' : 'M') + xs(pts[i].t).toFixed(1) + ' ' + ys(pts[i].v).toFixed(1);
    svg.appendChild(sv('path', {
      d: d, fill: 'none', stroke: PAL.fg, 'stroke-width': 1, 'stroke-opacity': 0.75,
      'stroke-linejoin': 'round', 'vector-effect': 'non-scaling-stroke'
    }));

    for (i = 0; i < crosses.length; i++) {
      var c = crosses[i];
      svg.appendChild(sv('circle', {
        cx: xs(c.t), cy: ys(0), r: 1.9, fill: c.to > 0 ? PAL.pos : PAL.neg,
        stroke: '#0b0e13', 'stroke-width': 0.6
      }, [sv('title', { text: 'смена знака ' + fmtDay(c.d) })]));
    }

    var ticks = yearTicks(t0, t1, 6);
    for (i = 0; i < ticks.length; i++) {
      svg.appendChild(sv('text', {
        x: xs(ticks[i].t), y: H - 4, 'text-anchor': 'middle', fill: PAL.dim,
        'font-size': 8, text: ticks[i].label
      }));
    }
    return svg;
  }

  // Цвет ячейки состояния. Логика REGIME.md §2: вола-шип при спокойных ОФЗ — окно входа,
  // тройной стресс у медведя — токсичная ячейка. Остальное — оттенки фона.
  function cellColor(code) {
    var c = parseCell(code);
    if (!c) return PAL.slate;
    if (c.vol === 1 && c.bond === 0) return PAL.pos;
    if (c.vol === 1 && c.bond === 1) return c.trend === 1 ? PAL.warn : PAL.neg;
    if (c.bond === 1) return c.trend === 1 ? '#6b5b2e' : '#6b4230';
    return c.trend === 1 ? '#2e7d5b' : PAL.slate;
  }
  function parseCell(code) {
    if (typeof code !== 'string') return null;
    var p = code.split('|');
    if (p.length < 3) return null;
    return {
      trend: p[0].trim() === 'bull' ? 1 : 0,
      vol: p[1].trim() === 'stress' ? 1 : 0,
      bond: p[2].trim() === 'stress' ? 1 : 0
    };
  }
  function cellWords(code) {
    return String(code || '').split('|').map(function (w) {
      w = w.trim(); return CELL_WORDS[w] || w;
    }).join(' · ');
  }

  // Лента ячеек: одинаковые подряд идущие коды склеиваются в отрезок — иначе на 2004+
  // получаются тысячи прямоугольников.
  function statesRibbon(series) {
    var rows = Array.isArray(series) ? series : [];
    var runs = [], i, t, code, last, diffs = [];
    for (i = 0; i < rows.length; i++) {
      var r = rows[i];
      if (!Array.isArray(r) || r.length < 2) continue;
      t = parseTs(r[0]);
      if (t == null) continue;
      code = String(r[1]);
      last = runs.length ? runs[runs.length - 1] : null;
      if (last) { diffs.push(t - last.t1); last.t1 = t; }
      if (last && last.code === code) continue;
      runs.push({ code: code, t0: t, t1: t, d0: r[0] });
    }
    if (!runs.length) return emptyChart('ещё не публиковалось');
    // Последний отрезок обрывается на своей же дате — растягиваем на типичный шаг ряда,
    // иначе текущее состояние на ленте не видно.
    diffs.sort(function (a, b) { return a - b; });
    var stepMs = diffs.length ? diffs[Math.floor(diffs.length / 2)] : 86400000;
    runs[runs.length - 1].t1 += Math.max(stepMs, 1);

    var W = fullChartW(), H = 44, L = 2, R = 2, T = 4, barH = 24;
    var t0 = runs[0].t0, t1 = runs[runs.length - 1].t1;
    var xs = function (t) { return t1 === t0 ? L : L + (W - L - R) * (t - t0) / (t1 - t0); };
    var svg = svgFrame(W, H);
    for (i = 0; i < runs.length; i++) {
      var x0 = xs(runs[i].t0), x1 = xs(runs[i].t1);
      svg.appendChild(sv('rect', {
        x: x0.toFixed(2), y: T, width: Math.max(0.6, x1 - x0).toFixed(2), height: barH,
        fill: cellColor(runs[i].code)
      }, [sv('title', { text: cellWords(runs[i].code) + ' — с ' + fmtDay(runs[i].d0) })]));
    }
    var ticks = yearTicks(t0, t1, 6);
    for (i = 0; i < ticks.length; i++) {
      svg.appendChild(sv('line', {
        x1: xs(ticks[i].t), x2: xs(ticks[i].t), y1: T + barH, y2: T + barH + 3,
        stroke: PAL.dim, 'stroke-width': 1, 'vector-effect': 'non-scaling-stroke'
      }));
      svg.appendChild(sv('text', {
        x: xs(ticks[i].t), y: H - 2, 'text-anchor': 'middle', fill: PAL.dim,
        'font-size': 8, text: ticks[i].label
      }));
    }
    return svg;
  }

  // Мини-график монитора: только форма ряда, без осей — числа даёт headline.
  function miniChart(pairs) {
    var pts = decimate(toPts(pairs), 160);
    if (pts.length < 2) return null;
    var W = 220, H = 54, pad = 3;
    var ex = extent(pts), lo = ex[0], hi = ex[1];
    var t0 = pts[0].t, t1 = pts[pts.length - 1].t;
    var xs = function (t) { return t1 === t0 ? pad : pad + (W - 2 * pad) * (t - t0) / (t1 - t0); };
    var ys = function (v) { return pad + (H - 2 * pad) * (hi - v) / (hi - lo); };
    var svg = svgFrame(W, H), i, d = '', area;
    for (i = 0; i < pts.length; i++) d += (i ? 'L' : 'M') + xs(pts[i].t).toFixed(1) + ' ' + ys(pts[i].v).toFixed(1);
    area = d + 'L' + xs(pts[pts.length - 1].t).toFixed(1) + ' ' + (H - pad) +
      'L' + xs(pts[0].t).toFixed(1) + ' ' + (H - pad) + 'Z';
    if (lo < 0 && hi > 0) {
      svg.appendChild(sv('line', {
        x1: pad, x2: W - pad, y1: ys(0), y2: ys(0), stroke: PAL.line,
        'stroke-width': 1, 'stroke-dasharray': '3 3', 'vector-effect': 'non-scaling-stroke'
      }));
    }
    svg.appendChild(sv('path', { d: area, fill: PAL.info, 'fill-opacity': 0.12 }));
    svg.appendChild(sv('path', {
      d: d, fill: 'none', stroke: PAL.info, 'stroke-width': 1.4,
      'stroke-linejoin': 'round', 'vector-effect': 'non-scaling-stroke'
    }));
    var last = pts[pts.length - 1];
    svg.appendChild(sv('circle', { cx: xs(last.t), cy: ys(last.v), r: 2.2, fill: PAL.info }));
    return { svg: svg, lo: lo, hi: hi, from: pts[0].d, to: last.d };
  }

  // Гейдж композита −3…+3 со стрелкой.
  function gauge(value) {
    var W = 320, H = 62, L = 14, R = 14, y = 30, barH = 12;
    var xs = function (v) { return L + (W - L - R) * (Math.max(-3, Math.min(3, v)) + 3) / 6; };
    var svg = svgFrame(W, H, 'chart chart--gauge'), i;
    for (i = 0; i < GAUGE_ZONES.length; i++) {
      var z = GAUGE_ZONES[i];
      svg.appendChild(sv('rect', {
        x: xs(z.from), y: y, width: xs(z.to) - xs(z.from), height: barH,
        fill: z.color, 'fill-opacity': z.alpha
      }));
    }
    svg.appendChild(sv('rect', {
      x: L, y: y, width: W - L - R, height: barH, fill: 'none',
      stroke: PAL.line, 'stroke-width': 1, 'vector-effect': 'non-scaling-stroke'
    }));
    for (i = 0; i < GAUGE_TICKS.length; i++) {
      var tv = GAUGE_TICKS[i];
      svg.appendChild(sv('line', {
        x1: xs(tv), x2: xs(tv), y1: y + barH, y2: y + barH + 3, stroke: PAL.dim,
        'stroke-width': 1, 'vector-effect': 'non-scaling-stroke'
      }));
      svg.appendChild(sv('text', {
        x: xs(tv), y: H - 2, 'text-anchor': 'middle', fill: PAL.dim, 'font-size': 8,
        text: (tv > 0 ? '+' : '') + tv
      }));
    }
    if (isNum(value)) {
      var x = xs(value);
      var col = value > 0.3 ? PAL.pos : value < -0.3 ? PAL.neg : PAL.fg;
      svg.appendChild(sv('path', {
        d: 'M' + x + ' ' + (y - 2) + 'L' + (x - 5) + ' ' + (y - 11) + 'L' + (x + 5) + ' ' + (y - 11) + 'Z',
        fill: col
      }));
      svg.appendChild(sv('text', {
        x: Math.max(L + 12, Math.min(W - R - 12, x)), y: y - 15, 'text-anchor': 'middle',
        fill: col, 'font-size': 11, 'font-weight': 'bold', text: num(value, 2, true)
      }));
    }
    return svg;
  }

  // ------------------------------------------------------- секция: вердикт
  function renderVerdict(data) {
    var v = data.verdict || {};
    var st = data.states || {};
    var cur = st.current || parseCell(v.cell_code) || {};
    // CONTRACT §3 кладёт since внутрь current, а §4 (API compute_states) — рядом.
    // Читаем оба места, чтобы подпись «с такого-то числа» не пропала при любом варианте.
    var since = (st.current && st.current.since) || st.since || {};
    var chips = [], i;

    for (i = 0; i < AXES.length; i++) {
      var ax = AXES[i], on = cur[ax.key] === 1, has = isNum(cur[ax.key]);
      chips.push(el('div', { 'class': 'chip chip--' + (has ? (on ? ax.onTone : ax.offTone) : 'mut') }, [
        el('span', { 'class': 'chip__k', text: ax.label }),
        el('span', { 'class': 'chip__v', text: has ? (on ? ax.on : ax.off) : '—' }),
        since[ax.key] ? el('span', { 'class': 'chip__since', text: 'с ' + fmtDay(since[ax.key]) }) : null
      ]));
    }
    var rp = RATE_PHASE[String(cur.rate_phase)] || null;
    chips.push(el('div', { 'class': 'chip chip--' + (rp ? rp.tone : 'mut') }, [
      el('span', { 'class': 'chip__k', text: 'Фаза ставки' }),
      el('span', { 'class': 'chip__v', text: rp ? rp.text : '—' })
    ]));

    var cs = v.cell_stats || {};
    var stats = el('div', { 'class': 'stats' }, [
      el('div', null, [
        el('div', { 'class': 'stat__k', text: 'Средний форвардный месяц' }),
        el('div', { 'class': 'stat__v num' + tone(cs.mean_fwd1m_pct), text: pct(cs.mean_fwd1m_pct, 2, true) })
      ]),
      el('div', null, [
        el('div', { 'class': 'stat__k', text: 'Доля плюсовых (hit)' }),
        el('div', { 'class': 'stat__v num', text: isNum(cs.hit) ? num(cs.hit * 100, 0) + '%' : '—' })
      ]),
      el('div', null, [
        el('div', { 'class': 'stat__k', text: 'Наблюдений' }),
        el('div', { 'class': 'stat__v num', text: isNum(cs.n) ? num(cs.n, 0) : '—' })
      ])
    ]);

    var coreVal = isNum(v.core_value) ? v.core_value : (data.core && data.core.value);
    var gaugeBox = el('div', { 'class': 'gauge-box' }, [
      el('div', { 'class': 'gauge-box__head' }, [
        el('div', null, [
          el('div', { 'class': 'stat__k', text: 'Композит ядра' }),
          el('div', { 'class': 'gauge-box__val num' + tone(coreVal), text: num(coreVal, 2, true) })
        ]),
        el('div', { 'class': 'gauge-box__lbl', text: v.core_label || (data.core && data.core.label) || '' })
      ]),
      gauge(coreVal)
    ]);

    return section('Вердикт', null, el('div', { 'class': 'verdict' }, [
      el('div', { 'class': 'chips' }, chips),
      el('div', { 'class': 'verdict__label', text: v.cell_label || (v.cell_code ? cellWords(v.cell_code) : 'ячейка не определена') }),
      v.cell_code ? el('div', { 'class': 'verdict__code', text: cellWords(v.cell_code) }) : null,
      stats,
      v.rule ? el('div', { 'class': 'rule' }, [
        el('span', { 'class': 'rule__k', text: 'Правило дня' }),
        document.createTextNode(v.rule)
      ]) : el('p', { 'class': 'empty', text: 'Правило дня не рассчитано' }),
      gaugeBox
    ]));
  }

  // ---------------------------------------------------------- секция: ядро
  function renderCore(data) {
    var core = data.core || {};
    var comps = Array.isArray(core.components) ? core.components : [];
    var cards = [], i;

    for (i = 0; i < comps.length; i++) {
      var c = comps[i] || {};
      var badge = tierBadge(c.tier);
      cards.push(el('article', { 'class': 'card' }, [
        el('div', { 'class': 'comp__head' }, [
          el('div', { 'class': 'comp__label', text: c.label || c.id || '—' }),
          el('div', { 'class': 'badges' }, badge.btn)
        ]),
        badge.note,
        el('div', { 'class': 'comp__z num' + tone(c.z), text: num(c.z, 2, true) }),
        el('div', { 'class': 'comp__raw', text: c.raw_fmt || (isNum(c.raw) ? smart(c.raw) : '—') }),
        el('div', { 'class': 'comp__spark' }, sparkline(c.spark)),
        el('div', { 'class': 'chart-cap', text: 'z-скор за 2 года' }),
        c.mechanism ? el('p', { 'class': 'comp__mech', text: c.mechanism }) : null,
        isNum(c.weight) ? el('p', { 'class': 'comp__meta', text: 'вес в композите ' + num(c.weight * 100, 0) + '%' }) : null
      ]));
    }
    if (!cards.length) cards.push(el('p', { 'class': 'empty', text: 'Компоненты ещё не публиковались' }));

    var h = core.health || {};
    var hTone = h.status === 'ok' ? 'tone--pos' : h.status === 'warn' ? 'tone--warn' : h.status === 'dead' ? 'tone--neg' : 'tone--mut';
    var health = el('article', { 'class': 'card' }, [
      el('div', { 'class': 'card__title', text: 'Здоровье модели' }),
      el('div', { 'class': 'health' }, [
        el('div', null, [
          el('div', { 'class': 'stat__k', text: 'Скользящий IC, 24 мес' }),
          el('div', { 'class': 'stat__v num' + tone(h.ic_24m), text: num(h.ic_24m, 2, true) })
        ]),
        el('div', null, [
          el('div', { 'class': 'stat__k', text: 'Наблюдений' }),
          el('div', { 'class': 'stat__v num', text: isNum(h.n) ? num(h.n, 0) : '—' })
        ]),
        el('div', null, [
          el('div', { 'class': 'stat__k', text: 'Статус' }),
          el('div', { 'class': 'stat__v ' + hTone, text: HEALTH_LABEL[h.status] || 'нет данных' })
        ])
      ]),
      el('p', { 'class': 'comp__meta', text: (h.status === 'dead'
        ? 'IC ушёл ниже нуля: это повод к ревизии состава ядра, а не к подгонке весов.'
        : (h.status === 'warn'
          ? 'IC около нуля: модель слабеет, но состав меняют только по итогам реколибровки — не по скользящему IC.'
          : 'IC положительный. Состав ядра фиксирован: отбор по скользящей результативности проверялся и проиграл.')) })
    ]);

    var ribbon = el('article', { 'class': 'card' }, [
      el('div', { 'class': 'card__head' }, [
        el('div', { 'class': 'card__title', text: 'Композит с 2004 года' }),
        core.sign_since ? el('span', { 'class': 'badge', text: 'знак с ' + fmtDay(core.sign_since) }) : null
      ]),
      coreRibbon(core.series),
      el('p', { 'class': 'chart-cap', text: 'Заливка — знак композита; точки на нуле — смены знака.' })
    ]);

    return section('Ядро', 'Слой 1: медленный композит, меняет знак примерно дважды в год', [
      el('div', { 'class': 'grid grid--3' }, cards),
      ribbon,
      health
    ]);
  }

  // ----------------------------------------------- секция: машина состояний
  function renderStates(data) {
    var st = data.states || {};
    var curCode = (data.verdict && data.verdict.cell_code) || null;
    var cells = Array.isArray(st.cells) ? st.cells : [];
    var legend = [], i;

    for (i = 0; i < cells.length; i++) {
      var c = cells[i] || {};
      var sw = el('span', { 'class': 'legend__sw' });
      // Цвет свотча ставим через CSSOM: в разметке style="" запрещён (CSP).
      sw.style.background = cellColor(c.code);
      legend.push(el('div', { 'class': 'legend__row' + (c.code === curCode ? ' is-current' : '') }, [
        sw,
        el('span', { text: cellWords(c.code) }),
        el('span', {
          'class': 'legend__stat',
          text: pct(c.mean_fwd1m_pct, 2, true) + ' · hit ' +
            (isNum(c.hit) ? num(c.hit * 100, 0) + '%' : '—') + ' · n ' + (isNum(c.n) ? num(c.n, 0) : '—')
        })
      ]));
    }

    var dists = Array.isArray(st.distances) ? st.distances : [];
    var distItems = [];
    for (i = 0; i < dists.length; i++) {
      var d = dists[i] || {};
      distItems.push(el('li', { 'class': 'list__item' }, [
        el('div', { 'class': 'list__row' }, [
          el('span', { 'class': 'list__name', text: DIST_LABEL[d.id] || d.id || '—' }),
          el('span', {
            'class': 'legend__stat num',
            text: (isNum(d.value) ? num(d.value, 2, true) : '—') + ' → порог ' + (isNum(d.threshold) ? num(d.threshold, 2, true) : '—')
          })
        ]),
        el('p', { 'class': 'list__text', text: d.text || '' }),
        isNum(d.gap_pct) ? el('p', { 'class': 'list__why', text: 'до переключения ' + num(Math.abs(d.gap_pct), 2) + ' п.п.' }) : null
      ]));
    }
    if (!distItems.length) distItems.push(el('li', { 'class': 'list__item empty', text: 'Расстояния ещё не рассчитаны' }));

    var sigs = Array.isArray(st.active_signals) ? st.active_signals : [];
    var sigItems = [];
    for (i = 0; i < sigs.length; i++) {
      var s = sigs[i] || {};
      sigItems.push(el('li', { 'class': 'list__item' }, [
        el('div', { 'class': 'list__row' }, [
          el('span', { 'class': 'list__name', text: s.label || s.id || '—' }),
          // Показываем именно z: вердикт считается по нему (sign x z), а сырое
          // значение без своей истории вводит в заблуждение — спред −4,35 п.п.
          // выглядит «против лонга», хотя для этого сигнала это максимум за годы.
          el('span', { 'class': 'list__text num' + tone(isNum(s.z) ? s.z * (s.sign || 1) : null),
                       text: isNum(s.z) ? 'z ' + num(s.z, 2, true) : num(s.value, 2, true) })
        ]),
        s.verdict ? el('p', { 'class': 'list__text', text: s.verdict
          + (isNum(s.value) ? ' · сейчас ' + num(s.value, 2, true) : '') }) : null,
        s.why ? el('p', { 'class': 'list__why', text: s.why }) : null
      ]));
    }
    if (!sigItems.length) {
      sigItems.push(el('li', { 'class': 'list__item empty', text: 'В текущей ячейке сигналы второго ряда не включены' }));
    }

    return section('Машина состояний', 'Слой 2: ворота риска. Три бита + фаза ставки', [
      el('article', { 'class': 'card' }, [
        el('div', { 'class': 'card__title', text: 'Ячейки с 2004 года' }),
        statesRibbon(st.series),
        legend.length ? el('div', { 'class': 'legend' }, legend)
          : el('p', { 'class': 'empty', text: 'Статистика ячеек ещё не публиковалась' })
      ]),
      el('article', { 'class': 'card' }, [
        el('div', { 'class': 'card__title', text: 'Расстояния до переключения' }),
        el('ul', { 'class': 'list' }, distItems)
      ]),
      el('article', { 'class': 'card' }, [
        el('div', { 'class': 'card__title', text: 'Активные сигналы второго ряда' }),
        el('ul', { 'class': 'list' }, sigItems)
      ])
    ]);
  }

  // ------------------------------------------------------ секция: мониторы
  function renderMonitors(data) {
    var mons = Array.isArray(data.monitors) ? data.monitors : [];
    var tiles = [], i;
    var now = Date.now();

    for (i = 0; i < mons.length; i++) {
      var m = mons[i] || {};
      var status = m.status || 'ok';
      var badge = tierBadge(m.tier);
      var cls = 'tile' + (m.tier === 'dead' ? ' tile--dead' : '') +
        (status === 'stale' || status === 'error' ? ' tile--stale' : '');
      var badges = [badge.btn];
      if (status !== 'ok') {
        badges.unshift(el('span', {
          'class': 'badge badge--' + (status === 'missing' ? 'missing' : status === 'error' ? 'error' : 'stale'),
          text: STATUS_LABEL[status] || status
        }));
      }
      var mini = m.payload && m.payload.series ? miniChart(m.payload.series) : null;
      var fetched = parseTs(m.fetched_at);
      var meta = [];
      if (m.asof) meta.push(el('span', { text: 'данные: ' + fmtDay(m.asof) }));
      if (fetched != null) meta.push(el('span', { text: 'опрошено ' + fmtAge(now - fetched) + ' назад' }));

      tiles.push(el('article', { 'class': cls }, [
        el('div', { 'class': 'tile__head' }, [
          el('div', { 'class': 'tile__title', text: m.title || m.id || '—' }),
          el('div', { 'class': 'badges' }, badges)
        ]),
        el('div', { 'class': 'tile__headline', text: m.headline || 'ещё не публиковалось' }),
        mini ? el('div', { 'class': 'tile__chart' }, [
          mini.svg,
          el('div', { 'class': 'tile__range', text: 'мин ' + smart(mini.lo) + ' · макс ' + smart(mini.hi) })
        ]) : null,
        m.note ? el('p', { 'class': 'tile__note', text: m.note }) : null,
        badge.note,
        meta.length ? el('div', { 'class': 'tile__meta' }, meta) : null
      ]));
    }
    if (!tiles.length) tiles.push(el('p', { 'class': 'empty', text: 'Мониторы ещё не публиковались' }));

    // Полоса источников: тайл может быть свежим, а его источник — протухшим (кэш).
    var srcWrap = null, src = data.sources;
    if (src && typeof src === 'object') {
      var chips = [], keys = Object.keys(src);
      for (i = 0; i < keys.length; i++) {
        var s = src[keys[i]] || {};
        var dot = el('span', { 'class': 'src__dot' });
        dot.style.background = s.status === 'ok' ? PAL.pos : s.status === 'stale' ? PAL.warn
          : s.status === 'error' ? PAL.neg : PAL.slate;
        // Статус пишем словом, а не только цветом точки: на солнце и в ч/б цвет не читается.
        chips.push(el('span', { 'class': 'src', title: STATUS_LABEL[s.status] || s.status || '' }, [
          dot,
          el('span', { text: keys[i] }),
          el('span', { 'class': 'tile__range', text: s.asof ? fmtDay(s.asof) : '—' }),
          s.status && s.status !== 'ok'
            ? el('span', { 'class': 'tone--warn', text: STATUS_LABEL[s.status] || s.status })
            : null
        ]));
      }
      if (chips.length) {
        srcWrap = el('article', { 'class': 'card' }, [
          el('div', { 'class': 'card__title', text: 'Источники' }),
          el('div', { 'class': 'sources' }, chips)
        ]);
      }
    }

    return section('Мониторы', 'Слой 3: наблюдение без предиктивных претензий', [
      el('div', { 'class': 'grid grid--mon' }, tiles),
      srcWrap
    ]);
  }

  // -------------------------------------------------------- секция: журнал
  function renderEvents(data) {
    var evs = Array.isArray(data.events) ? data.events.slice() : [];
    // Журнал читается сверху вниз от свежего: порядок в файле не гарантирован контрактом.
    evs.sort(function (a, b) { return (parseTs(b && b.ts) || 0) - (parseTs(a && a.ts) || 0); });
    if (!evs.length) {
      return section('Журнал событий', null, el('article', { 'class': 'card' },
        el('p', { 'class': 'empty', text: 'Событий ещё не было' })));
    }
    var list = el('ul', { 'class': 'list' }), hidden = [], i;
    for (i = 0; i < evs.length; i++) {
      var e = evs[i] || {};
      var ts = parseTs(e.ts);
      var item = el('li', { 'class': 'event' + (e.severity === 'warn' ? ' event--warn' : '') }, [
        el('span', { 'class': 'event__ts', text: ts != null ? fmtMskShort(ts) : '—' }),
        el('span', null, [
          el('span', { 'class': 'event__text', text: e.text || '' }),
          e.kind ? el('div', { 'class': 'event__kind', text: EVENT_KIND[e.kind] || e.kind }) : null
        ])
      ]);
      if (i >= EVENTS_VISIBLE) { item.hidden = true; hidden.push(item); }
      list.appendChild(item);
    }
    var card = el('article', { 'class': 'card' }, list);
    if (hidden.length) {
      var btn = el('button', { 'class': 'btn', type: 'button', text: 'Показать ещё ' + hidden.length });
      btn.addEventListener('click', function () {
        for (var j = 0; j < hidden.length; j++) hidden[j].hidden = false;
        btn.remove();
      });
      card.appendChild(btn);
    }
    return section('Журнал событий', null, card);
  }

  // --------------------------------------------------------------- рендер
  var root = document.getElementById('main');
  var ageEl = document.getElementById('age');
  var bannerEl = document.getElementById('banner');
  var footEl = document.getElementById('foot-line');
  var refreshBtn = document.getElementById('btn-refresh');

  var lastRaw = null;     // сырой текст ответа — по нему решаем, менялось ли что-то
  var lastData = null;
  var lastProblem = null; // {kind, ...} последней неудачной попытки
  var lastTryAt = 0;
  var busy = false;
  var renderedW = 0;      // ширина, под которую построены графики

  function renderAll(data) {
    renderedW = fullChartW();
    var frag = document.createDocumentFragment();
    append(frag, [
      renderVerdict(data),
      renderCore(data),
      renderStates(data),
      renderMonitors(data),
      renderEvents(data)
    ]);
    root.textContent = '';
    root.appendChild(frag);
    root.setAttribute('aria-busy', 'false');

    var bits = [];
    if (isNum(data.schema)) bits.push('схема ' + data.schema);
    if (data.run_mode) bits.push('режим прогона: ' + data.run_mode);
    if (data.asof_trading_day) bits.push('торговый день ' + fmtDayShort(data.asof_trading_day));
    footEl.textContent = bits.join(' · ');
  }

  function showBlocking(title, text, detail) {
    root.textContent = '';
    root.appendChild(el('article', { 'class': 'card' }, [
      el('div', { 'class': 'card__title', text: title }),
      el('p', { 'class': 'empty', text: text }),
      // Текст сервера показываем отдельной строкой и только как цитату: он приходит
      // снаружи и своим заголовком экрана быть не должен.
      detail ? el('p', { 'class': 'comp__meta', text: 'ответ сервера: ' + detail }) : null
    ]));
    root.setAttribute('aria-busy', 'false');
    footEl.textContent = '';
  }

  function setBanner(kind, title, text) {
    if (!kind) { bannerEl.hidden = true; bannerEl.textContent = ''; return; }
    bannerEl.className = 'banner banner--' + kind + ' banner--big';
    bannerEl.textContent = '';
    bannerEl.appendChild(el('strong', { 'class': 'banner__title', text: title }));
    bannerEl.appendChild(document.createTextNode(text));
    bannerEl.hidden = false;
  }

  // Возраст данных и плашки — единственное, что обновляется без перерисовки дерева.
  function updateFreshness() {
    var now = Date.now();
    if (!lastData) {
      ageEl.textContent = lastProblem ? problemText(lastProblem) : 'Загрузка…';
      ageEl.classList.remove('is-stale');
      return;
    }
    var gen = parseTs(lastData.generated_at);
    var limit = isNum(lastData.stale_after_minutes) ? lastData.stale_after_minutes : null;
    var ageMs = gen != null ? now - gen : null;
    var parts = [];
    parts.push(gen != null ? 'данные от ' + fmtMsk(gen) + ' МСК' : 'время публикации неизвестно');
    if (ageMs != null) parts.push(fmtAge(ageMs) + ' назад');
    ageEl.textContent = parts.join(' · ');

    var stale = ageMs != null && limit != null && ageMs > limit * 60000;
    ageEl.classList.toggle('is-stale', stale);

    if (lastProblem) {
      setBanner(lastProblem.kind === 'notready' ? 'warn' : 'err',
        'Обновление не удалось. ', problemText(lastProblem) + ' Показаны последние полученные данные.');
    } else if (stale) {
      setBanner('warn', 'Данные устарели: ' + fmtAge(ageMs) + '. ',
        'Сборщик молчит дольше нормы (' + num(limit, 0) + ' мин). Последняя публикация — ' +
        fmtMsk(gen) + ' МСК; цифры ниже относятся к ней, а не к текущему рынку.');
    } else {
      setBanner(null);
    }
  }

  function problemText(p) {
    if (!p) return '';
    if (p.kind === 'notready') return p.message || 'Сборщик ещё не публиковал данные.';
    if (p.kind === 'http') return 'Ответ сервера ' + p.status + '.';
    if (p.kind === 'badjson') return 'Ответ не разобрался как JSON.';
    return 'Сеть недоступна' + (p.message ? ' (' + p.message + ')' : '') + '.';
  }

  function fetchData() {
    // Метка времени в запросе: Pages/браузер иначе отдают кэш, а нам нужна свежесть.
    return fetch(DATA_URL + '?ts=' + Date.now(), {
      cache: 'no-store', headers: { 'Accept': 'application/json' }
    }).then(function (res) {
      if (res.status === 503) {
        // Фолбэк-текст функции читаем, но показываем как текст — доверять ему нельзя.
        return res.text().then(function (t) {
          var msg = null;
          try { var j = JSON.parse(t); msg = j && (j.message || j.error); } catch (e) { msg = null; }
          return { kind: 'notready', message: typeof msg === 'string' ? msg : null };
        }, function () { return { kind: 'notready' }; });
      }
      if (!res.ok) return { kind: 'http', status: res.status };
      return res.text().then(function (text) {
        var json;
        try { json = JSON.parse(text); } catch (e) { return { kind: 'badjson' }; }
        if (!json || typeof json !== 'object') return { kind: 'badjson' };
        return { kind: 'ok', text: text, data: json };
      });
    }, function (err) {
      return { kind: 'neterr', message: err && err.message ? err.message : null };
    });
  }

  function load() {
    if (busy) return;
    busy = true;
    refreshBtn.disabled = true;
    lastTryAt = Date.now();
    fetchData().then(function (res) {
      if (res.kind === 'ok') {
        lastProblem = null;
        if (res.text !== lastRaw) {
          lastRaw = res.text;
          lastData = res.data;
          // Скролл восстанавливаем сами: дерево заменяется целиком, а пользователь
          // может читать журнал в момент автообновления.
          var y = window.scrollY || window.pageYOffset || 0;
          renderAll(res.data);
          window.scrollTo(0, y);
        }
      } else {
        lastProblem = res;
        if (!lastData) {
          if (res.kind === 'notready') {
            showBlocking('Сборщик ещё не публиковал',
              'Панель появится после первого успешного прогона пайплайна.', res.message);
          } else {
            showBlocking('Данные недоступны', problemText(res));
          }
        }
      }
      updateFreshness();
    })['catch'](function (err) {
      lastProblem = { kind: 'neterr', message: err && err.message ? err.message : null };
      updateFreshness();
    }).then(function () {
      busy = false;
      refreshBtn.disabled = false;
    });
  }

  refreshBtn.addEventListener('click', load);

  // Поворот экрана меняет ширину — графики надо пересобрать. Порог 40 px: на телефоне
  // resize срабатывает ещё и на скрытие адресной строки (меняется только высота).
  var resizeTimer = null;
  window.addEventListener('resize', function () {
    if (resizeTimer) window.clearTimeout(resizeTimer);
    resizeTimer = window.setTimeout(function () {
      resizeTimer = null;
      if (!lastData || Math.abs(fullChartW() - renderedW) < 40) return;
      var y = window.scrollY || window.pageYOffset || 0;
      renderAll(lastData);
      window.scrollTo(0, y);
    }, 250);
  });

  // На телефоне вкладка часто «спит»: пока не видно — не дёргаем сеть, при возврате
  // обновляемся сразу, если прошлый заход был давно.
  document.addEventListener('visibilitychange', function () {
    if (!document.hidden && Date.now() - lastTryAt > REFRESH_MS / 2) load();
  });
  window.setInterval(function () { if (!document.hidden) load(); }, REFRESH_MS);
  window.setInterval(updateFreshness, AGE_TICK_MS);

  load();
})();
