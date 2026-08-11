/* MOEX Radar — графики. Чистый SVG, без библиотек и CDN (страница обязана
 * открываться под строгим CSP и без сети, кроме собственного /data/).
 *
 * Правила разметки, которым здесь следуют все формы:
 *   • линии 2px, сетка и оси — hairline на тон от поверхности, никаких пунктиров;
 *   • маркер конца ряда ≥8px и с кольцом цвета поверхности (2px), чтобы не слипался
 *     с линией под ним;
 *   • подписи выборочные: конец ряда и экстремум, а не число над каждой точкой;
 *   • у временных рядов по умолчанию крестовина с подсказкой — это HTML-график,
 *     а не картинка; подсказка ДОПОЛНЯЕТ, а не заменяет доступ к числу (есть режим
 *     таблицы), и клавиатура получает те же значения через стрелки.
 *
 * Ширину меряем у контейнера и перерисовываем на resize: тянуть SVG через
 * preserveAspectRatio="none" дешевле, но так растягивается и текст — подписи осей
 * поплыли бы по горизонтали.
 */
(function (global) {
  'use strict';

  var NS = 'http://www.w3.org/2000/svg';

  function el(name, attrs, kids) {
    var node = document.createElementNS(NS, name);
    if (attrs) for (var k in attrs) if (attrs[k] != null) node.setAttribute(k, String(attrs[k]));
    (kids || []).forEach(function (kid) { if (kid) node.appendChild(kid); });
    return node;
  }
  function h(name, attrs, kids) {
    var node = document.createElement(name);
    if (attrs) for (var k in attrs) {
      if (k === 'text') node.textContent = attrs[k];
      else if (k === 'html') node.innerHTML = attrs[k];
      else if (attrs[k] != null) node.setAttribute(k, String(attrs[k]));
    }
    (kids || []).forEach(function (kid) { if (kid) node.appendChild(kid); });
    return node;
  }
  function isNum(v) { return typeof v === 'number' && isFinite(v); }
  function clamp(v, a, b) { return v < a ? a : (v > b ? b : v); }

  /** Цвет из CSS-переменной: палитра живёт в одном месте и переключается с темой. */
  function tok(name, fallback) {
    var v = getComputedStyle(document.documentElement).getPropertyValue(name);
    return (v && v.trim()) || fallback || '#888';
  }

  function fmtNum(v, digits, sign) {
    if (!isNum(v)) return '—';
    var s = Math.abs(v).toLocaleString('ru-RU', {
      minimumFractionDigits: digits, maximumFractionDigits: digits
    });
    if (v < 0) return '−' + s;
    return (sign && v > 0 ? '+' : '') + s;
  }
  function fmtDay(iso) {
    if (!iso || iso.length < 10) return String(iso || '—');
    return iso.slice(8, 10) + '.' + iso.slice(5, 7) + '.' + iso.slice(0, 4);
  }
  function fmtMon(iso) {
    var m = ['янв', 'фев', 'мар', 'апр', 'мая', 'июн', 'июл', 'авг', 'сен', 'окт', 'ноя', 'дек'];
    if (!iso || iso.length < 7) return String(iso || '—');
    return m[parseInt(iso.slice(5, 7), 10) - 1] + ' ' + iso.slice(0, 4);
  }

  /* ─────────────────────────────────────────────── обвязка контейнера */

  var resizeHooks = [];
  var resizeTimer = null;
  global.addEventListener('resize', function () {
    if (resizeTimer) clearTimeout(resizeTimer);
    resizeTimer = setTimeout(function () {
      resizeHooks.forEach(function (fn) { try { fn(); } catch (e) { /* один график не роняет остальные */ } });
    }, 150);
  });
  function onResize(fn) { resizeHooks.push(fn); }
  function resetResize() { resizeHooks.length = 0; }

  /** Обёртка «график + подсказка»: возвращает {node, render(build)}.
   *
   * Ширину меряем ТОЛЬКО когда контейнер уже в документе. Первая редакция звала
   * build() сразу при сборке дерева — то есть до вставки, когда clientWidth равен
   * нулю, — и все графики молча рисовались по запасной ширине 320px, занимая
   * треть своей карточки. Отказ был тихий: ошибок нет, графики «есть», просто
   * маленькие. Поэтому здесь ResizeObserver: он срабатывает и на первой вставке
   * (даёт настоящую ширину), и на любом последующем изменении размера, включая
   * появление полосы прокрутки и поворот телефона.
   */
  function chartBox(height) {
    var tip = h('div', { 'class': 'tip', 'data-show': '0', role: 'status', 'aria-live': 'polite' });
    var box = h('div', { 'class': 'chart' }, [tip]);
    box.style.minHeight = height + 'px';
    return {
      node: box,
      tip: tip,
      render: function (build) {
        var lastW = 0;
        function draw() {
          var w = Math.round(box.clientWidth || box.getBoundingClientRect().width || 0);
          if (w < 80) return false;              // ещё не в документе — рисовать нечем
          if (Math.abs(w - lastW) < 2) return true;  // дрожание в 1px не повод перерисовывать
          lastW = w;
          var prev = box.querySelector('svg');
          if (prev) prev.remove();
          box.insertBefore(build(w), tip);
          return true;
        }
        box.__draw = draw;
        pending.push(box);
        // ResizeObserver — ТОЛЬКО на последующие изменения размера. Делать его
        // ответственным за первую отрисовку нельзя: его колбэки едут вместе с
        // циклом отрисовки страницы, а страница в скрытой (не композитящейся)
        // вкладке этот цикл не крутит — панель, открытая «в новой вкладке»,
        // показывала все девятнадцать графиков пустыми и без единой ошибки в
        // консоли. Первую отрисовку делает flush() сразу после вставки в DOM.
        if (typeof ResizeObserver === 'function') {
          var ro = new ResizeObserver(function () { draw(); });
          ro.observe(box);
        } else {
          onResize(function () { lastW = 0; draw(); });
        }
      }
    };
  }

  /** Дорисовать всё, что собрано, но ещё не нарисовано: зовётся ПОСЛЕ вставки
   *  дерева в документ, когда у контейнеров наконец появилась ширина. Что не
   *  получилось (узел ещё не в потоке), остаётся в очереди до следующего вызова. */
  var pending = [];
  function flush() {
    var rest = [];
    pending.forEach(function (box) {
      var ok = false;
      try { ok = box.__draw && box.__draw(); } catch (e) {
        // Один упавший график не имеет права унести с собой остальные и всю страницу.
        if (global.console && console.warn) console.warn('график не нарисовался:', e);
        ok = true;
      }
      if (!ok) rest.push(box);
    });
    pending = rest;
    return rest.length;
  }

  function showTip(tip, x, y, rows) {
    tip.innerHTML = '';
    rows.forEach(function (r) {
      if (r.title) { tip.appendChild(h('div', { 'class': 'tip__k', text: r.title })); return; }
      tip.appendChild(h('div', { 'class': 'tip__r' }, [
        h('span', { 'class': 'tip__k', text: r.k }),
        h('span', { 'class': 'tip__v', text: r.v, style: r.color ? 'color:' + r.color : null })
      ]));
    });
    tip.style.left = x + 'px';
    tip.style.top = y + 'px';
    tip.setAttribute('data-show', '1');
  }
  function hideTip(tip) { tip.setAttribute('data-show', '0'); }

  /* ────────────────────────────────── 1. Шкала композита (диверджент) */

  /** Полярность −3…+3: сегментированная дорожка, красная и синяя стороны,
   *  нейтральная середина, маркер текущего значения. Не «спидометр»: круглая
   *  шкала тратит место и хуже читается, чем прямая с подписанными зонами. */
  function polarityScale(value, opts) {
    opts = opts || {};
    var lo = -3, hi = 3, H = 66;
    var box = chartBox(H);
    box.render(function (W) {
      var padX = 10, padTop = 26, trackH = 12;
      var innerW = W - padX * 2;
      var x = function (v) { return padX + (clamp(v, lo, hi) - lo) / (hi - lo) * innerW; };
      var zones = [
        { a: -3, b: -1, c: tok('--neg'), o: 0.85, t: 'сильный шорт' },
        { a: -1, b: -0.3, c: tok('--neg'), o: 0.42, t: 'умеренный шорт' },
        { a: -0.3, b: 0.3, c: tok('--mid'), o: 1, t: 'нейтрально' },
        { a: 0.3, b: 1, c: tok('--pos'), o: 0.42, t: 'умеренный лонг' },
        { a: 1, b: 3, c: tok('--pos'), o: 0.85, t: 'сильный лонг' }
      ];
      var kids = [];
      zones.forEach(function (z) {
        // 2px зазор цвета поверхности между сегментами — разделяем пустотой, а не
        // обводкой: обводка утолщает марки и шумит на мелком масштабе.
        var x0 = x(z.a), x1 = x(z.b);
        kids.push(el('rect', {
          x: x0 + 1, y: padTop, width: Math.max(1, x1 - x0 - 2), height: trackH,
          rx: 3, fill: z.c, 'fill-opacity': z.o
        }));
      });
      [-3, -1, 0, 1, 3].forEach(function (v) {
        kids.push(el('text', {
          x: x(v), y: padTop + trackH + 15, 'text-anchor': 'middle', 'class': 'tick'
        }, [document.createTextNode(v > 0 ? '+' + v : String(v))]));
      });
      if (isNum(value)) {
        var mx = x(value);
        kids.push(el('line', {
          x1: mx, y1: padTop - 7, x2: mx, y2: padTop + trackH + 4,
          stroke: tok('--ink'), 'stroke-width': 2.5, 'stroke-linecap': 'round'
        }));
        kids.push(el('circle', {
          cx: mx, cy: padTop - 10, r: 4.5, fill: tok('--ink'),
          stroke: tok('--surface'), 'stroke-width': 2
        }));
      }
      return el('svg', {
        'class': 'fig', width: W, height: H, viewBox: '0 0 ' + W + ' ' + H,
        role: 'img',
        'aria-label': 'Шкала композита от минус трёх до плюс трёх, текущее значение ' +
          fmtNum(value, 2, true)
      }, kids);
    });
    return box.node;
  }

  /* ─────────────────────────── 2. История со знаковой заливкой + крестовина */

  /** Ряд [[дата, значение]] с нулевой линией: заливка выше нуля — синяя,
   *  ниже — красная (диверджент по полярности). Точки смены знака подписаны
   *  выборочно; каждое значение доступно в подсказке и в режиме таблицы. */
  function signedHistory(series, opts) {
    opts = opts || {};
    var H = opts.height || 190;
    var box = chartBox(H);
    var pts = (series || []).filter(function (r) { return r && isNum(+r[1]); })
      .map(function (r) { return [String(r[0]), +r[1]]; });
    box.render(function (W) {
      if (!pts.length) return el('svg', { width: W, height: H });
      var padL = 30, padR = 12, padT = 12, padB = 22;
      var iw = W - padL - padR, ih = H - padT - padB;
      var vmax = 0;
      pts.forEach(function (p) { vmax = Math.max(vmax, Math.abs(p[1])); });
      vmax = Math.max(0.5, Math.ceil(vmax * 2) / 2);
      var x = function (i) { return padL + (pts.length === 1 ? iw / 2 : i / (pts.length - 1) * iw); };
      var y = function (v) { return padT + (1 - (v + vmax) / (2 * vmax)) * ih; };
      var y0 = y(0);
      var kids = [];

      [vmax, 0, -vmax].forEach(function (v) {
        kids.push(el('line', { x1: padL, y1: y(v), x2: W - padR, y2: y(v), 'class': v === 0 ? 'axisline' : 'gridline' }));
        kids.push(el('text', { x: padL - 6, y: y(v) + 3.5, 'text-anchor': 'end', 'class': 'tick' },
          [document.createTextNode(fmtNum(v, v % 1 ? 1 : 0, true))]));
      });

      // Заливка знаком: два пути, каждый обрезан своей половиной плоскости.
      var line = pts.map(function (p, i) { return (i ? 'L' : 'M') + x(i).toFixed(1) + ' ' + y(p[1]).toFixed(1); }).join(' ');
      var area = line + ' L' + x(pts.length - 1).toFixed(1) + ' ' + y0.toFixed(1) +
        ' L' + x(0).toFixed(1) + ' ' + y0.toFixed(1) + ' Z';
      var uid = 'clip' + Math.random().toString(36).slice(2, 8);
      kids.push(el('defs', null, [
        el('clipPath', { id: uid + 'p' }, [el('rect', { x: 0, y: padT - 2, width: W, height: y0 - padT + 2 })]),
        el('clipPath', { id: uid + 'n' }, [el('rect', { x: 0, y: y0, width: W, height: H - y0 })])
      ]));
      kids.push(el('path', { d: area, fill: tok('--pos'), 'fill-opacity': 0.16, 'clip-path': 'url(#' + uid + 'p)' }));
      kids.push(el('path', { d: area, fill: tok('--neg'), 'fill-opacity': 0.16, 'clip-path': 'url(#' + uid + 'n)' }));
      kids.push(el('path', { d: line, 'class': 'series', stroke: tok('--pos'), 'clip-path': 'url(#' + uid + 'p)' }));
      kids.push(el('path', { d: line, 'class': 'series', stroke: tok('--neg'), 'clip-path': 'url(#' + uid + 'n)' }));

      // Годовые отметки — по одной на 3–4 года, чтобы подписи не сталкивались.
      var years = {}, order = [];
      pts.forEach(function (p, i) { var yr = p[0].slice(0, 4); if (!(yr in years)) { years[yr] = i; order.push(yr); } });
      var step = Math.max(1, Math.round(order.length / Math.max(3, Math.floor(iw / 90))));
      order.forEach(function (yr, k) {
        if (k % step) return;
        var i = years[yr];
        kids.push(el('text', { x: x(i), y: H - 6, 'text-anchor': 'middle', 'class': 'tick' },
          [document.createTextNode(yr)]));
      });

      // Смены знака: выборочные подписи вместо числа над каждой точкой.
      for (var i = 1; i < pts.length; i++) {
        if ((pts[i][1] >= 0) !== (pts[i - 1][1] >= 0)) {
          kids.push(el('circle', {
            cx: x(i), cy: y0, r: 3, fill: tok('--ink-3'),
            stroke: tok('--surface'), 'stroke-width': 2
          }));
        }
      }

      var last = pts[pts.length - 1];
      kids.push(el('circle', {
        cx: x(pts.length - 1), cy: y(last[1]), r: 4.5,
        fill: last[1] >= 0 ? tok('--pos') : tok('--neg'),
        stroke: tok('--surface'), 'stroke-width': 2
      }));

      var cross = el('line', { 'class': 'cross', x1: 0, y1: padT, x2: 0, y2: padT + ih, opacity: 0 });
      var focus = el('circle', { r: 4.5, fill: tok('--ink'), stroke: tok('--surface'), 'stroke-width': 2, opacity: 0 });
      kids.push(cross, focus);

      var hit = el('rect', { 'class': 'hit', x: padL, y: padT, width: iw, height: ih });
      kids.push(hit);

      var svg = el('svg', {
        'class': 'fig', width: W, height: H, viewBox: '0 0 ' + W + ' ' + H,
        role: 'img', tabindex: '0',
        'aria-label': (opts.aria || 'Историческая динамика') + ': ' + pts.length +
          ' точек с ' + fmtMon(pts[0][0]) + ' по ' + fmtMon(last[0]) +
          ', последнее значение ' + fmtNum(last[1], 2, true)
      }, kids);

      var tip = box.tip;
      function pick(clientX) {
        var r = svg.getBoundingClientRect();
        var rel = clamp(clientX - r.left, padL, padL + iw);
        var i = Math.round((rel - padL) / iw * (pts.length - 1));
        return clamp(i, 0, pts.length - 1);
      }
      function show(i) {
        var px = x(i), py = y(pts[i][1]);
        cross.setAttribute('x1', px); cross.setAttribute('x2', px); cross.setAttribute('opacity', 1);
        focus.setAttribute('cx', px); focus.setAttribute('cy', py); focus.setAttribute('opacity', 1);
        showTip(tip, px, py, [
          { title: fmtMon(pts[i][0]) },
          { k: opts.label || 'значение', v: fmtNum(pts[i][1], 2, true),
            color: pts[i][1] >= 0 ? tok('--pos') : tok('--neg') }
        ]);
      }
      function hide() { cross.setAttribute('opacity', 0); focus.setAttribute('opacity', 0); hideTip(tip); }
      hit.addEventListener('pointermove', function (e) { show(pick(e.clientX)); });
      hit.addEventListener('pointerleave', hide);
      var kb = pts.length - 1;
      svg.addEventListener('keydown', function (e) {
        if (e.key !== 'ArrowLeft' && e.key !== 'ArrowRight') return;
        e.preventDefault();
        kb = clamp(kb + (e.key === 'ArrowRight' ? 1 : -1), 0, pts.length - 1);
        show(kb);
      });
      svg.addEventListener('blur', hide);
      return svg;
    });
    return box.node;
  }

  /* ───────────────────────────────────────────── 3. Спарклайн компонента */

  function spark(series, color, opts) {
    opts = opts || {};
    var H = opts.height || 46;
    var box = chartBox(H);
    var pts = (series || []).filter(function (r) { return r && isNum(+r[1]); })
      .map(function (r) { return [String(r[0]), +r[1]]; });
    box.render(function (W) {
      if (pts.length < 2) {
        return el('svg', { 'class': 'fig', width: W, height: H }, [
          el('text', { x: 0, y: H / 2, 'class': 'tick' }, [document.createTextNode('истории пока нет')])
        ]);
      }
      var padR = 7, padY = 6, iw = W - padR - 1, ih = H - padY * 2;
      var lo = Infinity, hi = -Infinity;
      pts.forEach(function (p) { lo = Math.min(lo, p[1]); hi = Math.max(hi, p[1]); });
      if (hi - lo < 1e-9) { hi += 0.5; lo -= 0.5; }
      var x = function (i) { return 1 + i / (pts.length - 1) * iw; };
      var y = function (v) { return padY + (1 - (v - lo) / (hi - lo)) * ih; };
      var d = pts.map(function (p, i) { return (i ? 'L' : 'M') + x(i).toFixed(1) + ' ' + y(p[1]).toFixed(1); }).join(' ');
      var kids = [];
      if (lo < 0 && hi > 0) {
        kids.push(el('line', { x1: 1, y1: y(0), x2: 1 + iw, y2: y(0), 'class': 'gridline' }));
      }
      kids.push(el('path', { d: d, 'class': 'series', stroke: color }));
      var last = pts[pts.length - 1];
      kids.push(el('circle', {
        cx: x(pts.length - 1), cy: y(last[1]), r: 4, fill: color,
        stroke: tok('--surface'), 'stroke-width': 2
      }));
      return el('svg', {
        'class': 'fig', width: W, height: H, viewBox: '0 0 ' + W + ' ' + H, role: 'img',
        'aria-label': (opts.aria || 'Динамика') + ' за ' + pts.length + ' точек, ' +
          'от ' + fmtNum(pts[0][1], 2, true) + ' до ' + fmtNum(last[1], 2, true)
      }, kids);
    });
    return box.node;
  }

  /* ─────────────────────────────────────────── 4. Лента ячеек состояния */

  /** Непрерывная тепловая полоса: каждый месяц — сегмент, цвет по средней
   *  форвардной доходности ячейки (диверджент, пять ступеней). Зазоров между
   *  сегментами нет намеренно: это непрерывная шкала времени, а не набор
   *  столбиков — разрывы читались бы как пропуски в данных. */
  function stateRibbon(series, cells, opts) {
    opts = opts || {};
    var H = opts.height || 74;
    var box = chartBox(H);
    var byCode = {};
    (cells || []).forEach(function (c) { byCode[c.code] = c; });
    var pts = (series || []).filter(function (r) { return r && r[1]; });

    function color(code) {
      var c = byCode[code];
      if (!c || !isNum(c.mean_fwd1m_pct)) return tok('--mid');
      var v = c.mean_fwd1m_pct;
      if (v <= -1.5) return tok('--neg');
      if (v < 0) return 'color-mix(in srgb, ' + tok('--neg') + ' 45%, ' + tok('--mid') + ')';
      if (v < 0.8) return tok('--mid');
      if (v < 1.8) return 'color-mix(in srgb, ' + tok('--pos') + ' 45%, ' + tok('--mid') + ')';
      return tok('--pos');
    }

    box.render(function (W) {
      if (!pts.length) return el('svg', { 'class': 'fig', width: W, height: H });
      var padT = 8, bandH = 30, padL = 0;
      var iw = W - padL;
      var seg = iw / pts.length;
      var kids = [];
      pts.forEach(function (p, i) {
        kids.push(el('rect', {
          x: padL + i * seg, y: padT, width: Math.max(0.7, seg + 0.4), height: bandH,
          fill: color(p[1])
        }));
      });
      // Годовые подписи под лентой — редкие, чтобы не столкнулись.
      var years = {}, order = [];
      pts.forEach(function (p, i) { var yr = String(p[0]).slice(0, 4); if (!(yr in years)) { years[yr] = i; order.push(yr); } });
      var step = Math.max(1, Math.round(order.length / Math.max(3, Math.floor(iw / 78))));
      order.forEach(function (yr, k) {
        if (k % step) return;
        var px = padL + years[yr] * seg;
        kids.push(el('line', { x1: px, y1: padT + bandH, x2: px, y2: padT + bandH + 4, 'class': 'axisline' }));
        kids.push(el('text', { x: px, y: padT + bandH + 17, 'text-anchor': 'middle', 'class': 'tick' },
          [document.createTextNode(yr)]));
      });
      var lastX = padL + (pts.length - 1) * seg + seg / 2;
      kids.push(el('path', {
        d: 'M' + lastX + ' ' + (padT - 2) + ' l4 -6 l-8 0 Z', fill: tok('--ink')
      }));

      var cross = el('rect', { x: 0, y: padT, width: Math.max(1.5, seg), height: bandH, fill: 'none', stroke: tok('--ink'), 'stroke-width': 1.5, opacity: 0 });
      kids.push(cross);
      var hit = el('rect', { 'class': 'hit', x: padL, y: padT, width: iw, height: bandH });
      kids.push(hit);

      var svg = el('svg', {
        'class': 'fig', width: W, height: H, viewBox: '0 0 ' + W + ' ' + H, role: 'img',
        'aria-label': 'Лента состояний рынка с ' + String(pts[0][0]).slice(0, 4) +
          ' по ' + String(pts[pts.length - 1][0]).slice(0, 4) +
          '. Текущая ячейка: ' + (byCode[pts[pts.length - 1][1]] || {}).label
      }, kids);

      var tip = box.tip;
      hit.addEventListener('pointermove', function (e) {
        var r = svg.getBoundingClientRect();
        var i = clamp(Math.floor((e.clientX - r.left - padL) / seg), 0, pts.length - 1);
        var c = byCode[pts[i][1]] || {};
        cross.setAttribute('x', padL + i * seg); cross.setAttribute('opacity', 1);
        showTip(tip, padL + i * seg + seg / 2, padT + bandH / 2, [
          { title: fmtMon(pts[i][0]) },
          { k: 'ячейка', v: c.label || pts[i][1] },
          { k: 'ср. месяц', v: isNum(c.mean_fwd1m_pct) ? fmtNum(c.mean_fwd1m_pct, 2, true) + '%' : '—',
            color: isNum(c.mean_fwd1m_pct) ? (c.mean_fwd1m_pct >= 0 ? tok('--pos') : tok('--neg')) : null },
          { k: 'наблюдений', v: isNum(c.n) ? String(c.n) : '—' }
        ]);
      });
      hit.addEventListener('pointerleave', function () { cross.setAttribute('opacity', 0); hideTip(tip); });
      return svg;
    });
    return box.node;
  }

  /* ───────────────────────────────────── 5. Расстояние до переключения */

  /** Текущее значение против порога: дорожка, засечка порога, маркер значения.
   *  Заливка показывает, с какой стороны порога мы находимся. */
  function thresholdBar(value, threshold, opts) {
    opts = opts || {};
    var H = 34;
    var box = chartBox(H);
    box.render(function (W) {
      if (!isNum(value) || !isNum(threshold)) return el('svg', { 'class': 'fig', width: W, height: H });
      var span = Math.max(Math.abs(value - threshold) * 2.4, Math.abs(threshold) * 0.5, 1);
      var mid = threshold, lo = mid - span, hi = mid + span;
      var padX = 6, iw = W - padX * 2, trackY = 12, trackH = 8;
      var x = function (v) { return padX + clamp((v - lo) / (hi - lo), 0, 1) * iw; };
      var beyond = opts.invert ? value <= threshold : value >= threshold;
      var kids = [
        el('rect', { x: padX, y: trackY, width: iw, height: trackH, rx: 4, fill: tok('--inset') }),
        el('rect', {
          x: Math.min(x(value), x(threshold)), y: trackY,
          width: Math.max(2, Math.abs(x(value) - x(threshold))), height: trackH, rx: 4,
          fill: beyond ? tok('--neg') : tok('--pos'), 'fill-opacity': .55
        }),
        el('line', { x1: x(threshold), y1: trackY - 5, x2: x(threshold), y2: trackY + trackH + 5, stroke: tok('--ink-3'), 'stroke-width': 2 }),
        el('circle', { cx: x(value), cy: trackY + trackH / 2, r: 5, fill: tok('--ink'), stroke: tok('--surface'), 'stroke-width': 2 }),
        el('text', { x: x(threshold), y: trackY + trackH + 17, 'text-anchor': 'middle', 'class': 'tick' },
          [document.createTextNode('порог ' + fmtNum(threshold, opts.digits == null ? 1 : opts.digits, false))])
      ];
      return el('svg', {
        'class': 'fig', width: W, height: H, viewBox: '0 0 ' + W + ' ' + H, role: 'img',
        'aria-label': 'Текущее ' + fmtNum(value, 1, true) + ' при пороге ' + fmtNum(threshold, 1, true)
      }, kids);
    });
    return box.node;
  }

  /* ──────────────────────────────────────── 6. Мини-ряд для монитора */

  function miniSeries(series, opts) {
    opts = opts || {};
    var H = opts.height || 40;
    var color = opts.color || tok('--s1');
    var box = chartBox(H);
    var pts = (series || []).filter(function (r) { return r && isNum(+r[1]); })
      .map(function (r) { return [String(r[0]), +r[1]]; });
    box.render(function (W) {
      if (pts.length < 2) return el('svg', { 'class': 'fig', width: W, height: H });
      var padY = 5, iw = W - 6, ih = H - padY * 2;
      var lo = Infinity, hi = -Infinity;
      pts.forEach(function (p) { lo = Math.min(lo, p[1]); hi = Math.max(hi, p[1]); });
      if (hi - lo < 1e-9) { hi += 1; lo -= 1; }
      var x = function (i) { return 1 + i / (pts.length - 1) * iw; };
      var y = function (v) { return padY + (1 - (v - lo) / (hi - lo)) * ih; };
      var d = pts.map(function (p, i) { return (i ? 'L' : 'M') + x(i).toFixed(1) + ' ' + y(p[1]).toFixed(1); }).join(' ');
      var kids = [];
      if (opts.zero && lo < 0 && hi > 0) kids.push(el('line', { x1: 1, y1: y(0), x2: 1 + iw, y2: y(0), 'class': 'gridline' }));
      kids.push(el('path', { d: d, 'class': 'series', stroke: color, 'stroke-width': 1.75 }));
      var last = pts[pts.length - 1];
      kids.push(el('circle', { cx: x(pts.length - 1), cy: y(last[1]), r: 3.5, fill: color, stroke: tok('--surface'), 'stroke-width': 2 }));
      var svg = el('svg', {
        'class': 'fig', width: W, height: H, viewBox: '0 0 ' + W + ' ' + H, role: 'img',
        'aria-label': (opts.aria || 'Динамика показателя') + ': от ' + fmtNum(pts[0][1], 1, false) +
          ' до ' + fmtNum(last[1], 1, false)
      }, kids);
      var tip = box.tip;
      var hit = el('rect', { 'class': 'hit', x: 0, y: 0, width: W, height: H });
      svg.appendChild(hit);
      hit.addEventListener('pointermove', function (e) {
        var r = svg.getBoundingClientRect();
        var i = clamp(Math.round((e.clientX - r.left - 1) / iw * (pts.length - 1)), 0, pts.length - 1);
        showTip(tip, x(i), y(pts[i][1]), [
          { title: fmtDay(pts[i][0]) },
          { k: opts.label || 'значение', v: fmtNum(pts[i][1], opts.digits == null ? 1 : opts.digits, false) + (opts.unit || '') }
        ]);
      });
      hit.addEventListener('pointerleave', function () { hideTip(tip); });
      return svg;
    });
    return box.node;
  }

  /* ──────────────────────────── 7. Потоки по категориям (расходящиеся) */

  /** Столбики по месяцам вокруг нуля: покупки вверх, продажи вниз. Одна
   *  категория за раз (выбранная), остальные — в подсказке: восемь цветных
   *  стопок на 260 пикселях не читаются никем. */
  function flowBars(months, values, opts) {
    opts = opts || {};
    var H = opts.height || 58;
    var box = chartBox(H);
    box.render(function (W) {
      var vals = (values || []).map(function (v) { return isNum(+v) ? +v : null; });
      if (!vals.length) return el('svg', { 'class': 'fig', width: W, height: H });
      var m = 0;
      vals.forEach(function (v) { if (isNum(v)) m = Math.max(m, Math.abs(v)); });
      if (m <= 0) m = 1;
      var padY = 6, ih = H - padY * 2, y0 = padY + ih / 2;
      var bw = Math.max(3, (W - 4) / vals.length - 2);
      var kids = [el('line', { x1: 0, y1: y0, x2: W, y2: y0, 'class': 'axisline' })];
      vals.forEach(function (v, i) {
        if (!isNum(v)) return;
        var hgt = Math.abs(v) / m * (ih / 2 - 1);
        var xx = 2 + i * ((W - 4) / vals.length);
        kids.push(el('rect', {
          x: xx, y: v >= 0 ? y0 - hgt : y0, width: bw, height: Math.max(1.5, hgt), rx: 2,
          fill: v >= 0 ? tok('--pos') : tok('--neg'), 'fill-opacity': .8
        }));
      });
      var svg = el('svg', {
        'class': 'fig', width: W, height: H, viewBox: '0 0 ' + W + ' ' + H, role: 'img',
        'aria-label': (opts.aria || 'Потоки по месяцам') + ', ' + vals.length + ' месяцев'
      }, kids);
      var tip = box.tip;
      var hit = el('rect', { 'class': 'hit', x: 0, y: 0, width: W, height: H });
      svg.appendChild(hit);
      hit.addEventListener('pointermove', function (e) {
        var r = svg.getBoundingClientRect();
        var i = clamp(Math.floor((e.clientX - r.left - 2) / ((W - 4) / vals.length)), 0, vals.length - 1);
        if (!isNum(vals[i])) { hideTip(tip); return; }
        showTip(tip, 2 + i * ((W - 4) / vals.length) + bw / 2, y0, [
          { title: fmtMon(months[i]) },
          { k: opts.label || 'нетто', v: fmtNum(vals[i], 1, true) + (opts.unit || ''),
            color: vals[i] >= 0 ? tok('--pos') : tok('--neg') }
        ]);
      });
      hit.addEventListener('pointerleave', function () { hideTip(tip); });
      return svg;
    });
    return box.node;
  }

  global.Charts = {
    el: el, h: h, tok: tok, isNum: isNum, clamp: clamp,
    fmtNum: fmtNum, fmtDay: fmtDay, fmtMon: fmtMon,
    polarityScale: polarityScale, signedHistory: signedHistory, spark: spark,
    stateRibbon: stateRibbon, thresholdBar: thresholdBar,
    miniSeries: miniSeries, flowBars: flowBars,
    flush: flush, resetResize: resetResize
  };
})(window);
