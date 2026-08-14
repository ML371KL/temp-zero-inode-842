/**
 * Общий фильтр всех запросов к сайту. Делает ровно две вещи, которые нельзя сделать
 * ни в статике, ни внутри `functions/data/[[path]].js`.
 *
 * 1. ДЕРЖИТ ГРАНИЦУ `/data/`. Обещание в шапке функции — «под /data/ нет ни одного
 * пути, который отвечал бы вёрсткой» — не выполнялось: до функции доезжал только
 * канонический путь, а `/data//data.json`, `/data/../lease.json`, `/data/%2e%2e/…`
 * и `/data//raw/imoex.json` в маршрутизацию `[[path]]` не попадали вовсе и уходили
 * к статике, а та на неизвестный путь отдаёт **200 и HTML главной страницы**
 * (проверено curl-ом на проде и один в один воспроизведено в `wrangler pages dev`).
 * Программа, склеившая базу с ключом и получившая лишний слеш, читала «успех» и
 * разбирала вёрстку как JSON — ровно та авария, которой соседняя панель заплатила
 * дважды за двое суток: движок алертов девять часов не проверял ни одного правила,
 * а сторож свежести объявил три живые панели мёртвыми.
 * Здесь путь под `/data/` либо КАНОНИЧЕН (сегменты из букв, цифр, `.`, `_`, `-` —
 * ни пустых, ни `.`/`..`, ни процент-кодирования) и уходит дальше в функцию, либо
 * получает такой же JSON-404, как несуществующий объект. Никакой нормализации
 * «догадайся, что имел в виду клиент»: что не совпало с каноном, того не существует —
 * тот же принцип, что и у белого списка ключей.
 *
 * 2. СТАВИТ ЗАГОЛОВКИ БЕЗОПАСНОСТИ на ВСЕ ответы. Файл `web/_headers` не годится:
 * Pages накладывает его только на статику, а ответы функций — единственное место,
 * где отражается пользовательский ввод (поле `requested` в 404), — остались бы без
 * `nosniff`. Здесь один источник правды на обе половины сайта.
 *
 * ЦЕНА. Каждый запрос, включая статику, теперь проходит через воркер. Это плата за
 * то, что граница `/data/` держится на КОДЕ, а не на догадках о том, как край
 * разберёт путь.
 */

// Хэш инлайн-скрипта темы из web/index.html (он ставит тему ДО первой отрисовки,
// иначе ночью моргает светлая). Считается по содержимому <script> с переводами строк
// LF: в git файлы лежат с LF (.gitattributes), рабочая копия на Windows может быть с
// CRLF — хэш от CRLF-версии не совпадёт с тем, что увидит браузер. Пересчитывать при
// любой правке того скрипта; расхождение ловит шаг «CSP: хэш инлайн-скрипта» в CI и
// печатает готовую строку на замену.
const THEME_SCRIPT_HASH = "sha256-PA8O/+/oTkckcd/QPa19vldLqOXpYDStFgytBGbPYIg=";

// 'unsafe-inline' в style-src — вынужденно и осознанно: web/charts.js ставит атрибут
// style через setAttribute в шести местах (отступы карточек и сетка), и без него
// вёрстка разъезжается. Убрать можно только вместе с переводом тех шести мест на
// классы. Всё остальное строго: страница не грузит ни одного стороннего ресурса
// (проверено — внешних адресов в web/ нет, шрифты системные), поэтому default-src
// 'none' ничего не ломает, а frame-ancestors закрывает вкладывание панели в чужой
// iframe.
const CSP = [
  "default-src 'none'",
  `script-src 'self' '${THEME_SCRIPT_HASH}'`,
  "style-src 'self' 'unsafe-inline'",
  "img-src 'self'",
  "connect-src 'self'",
  "base-uri 'none'",
  "form-action 'none'",
  "frame-ancestors 'none'",
].join("; ");

const SECURITY_HEADERS = {
  "content-security-policy": CSP,
  // Статике Pages ставит nosniff сам, ответам функции — нет. Разнобой на одном
  // периметре опаснее отсутствия: он создаёт ложную уверенность, что заголовок есть.
  "x-content-type-options": "nosniff",
  "referrer-policy": "strict-origin-when-cross-origin",
  // Дублирует frame-ancestors для старых движков, которые про CSP3 не знают.
  "x-frame-options": "DENY",
};

// Сегмент пути в бакете: имена объектов витрины — обычные ASCII-имена файлов.
// Начинается с буквы или цифры, поэтому `.`, `..` и пустой сегмент не проходят,
// а `%2e%2e`, `%2F` и прочие кодировки отсекаются символом `%`.
const SEGMENT = /^[A-Za-z0-9][A-Za-z0-9._-]*$/;

function isDataRequest(pathname) {
  return pathname === "/data" || pathname.startsWith("/data/");
}

function isCanonicalDataPath(pathname) {
  // `/data` и `/data/` канонические: ключ пустой, и функция честно ответит 404 JSON
  // со списком доступных объектов.
  if (pathname === "/data" || pathname === "/data/") return true;
  return pathname.slice("/data/".length).split("/").every((seg) => SEGMENT.test(seg));
}

function notCanonical(request, pathname) {
  const body = JSON.stringify({
    error: "no such data object",
    requested: pathname,
    hint: "путь под /data/ должен быть каноническим: без пустых сегментов, «..» и "
        + "процент-кодирования — например /data/data.json",
  });
  return new Response(request.method === "HEAD" ? null : body, {
    status: 404,
    headers: {
      "content-type": "application/json; charset=utf-8",
      // Тот же no-store, что и у функции: ответ об ошибке, положенный в кэш, однажды
      // переживёт починку пути.
      "cache-control": "no-store",
      ...SECURITY_HEADERS,
    },
  });
}

export async function onRequest({ request, next }) {
  const { pathname } = new URL(request.url);

  // Схлопнутый путь — чтобы поймать обе формы ошибки склейки: база с конечным слешем
  // плюс ключ с ведущим («//data/data.json») и база «…/data/» плюс «/data.json»
  // («/data//data.json»). Край Cloudflare ни ту, ни другую не нормализует и уводит
  // запрос на статику; для клиента это тот же вопрос «дай данные», и ответ обязан
  // быть данными или честным JSON-404, но не вёрсткой.
  const collapsed = pathname.replace(/\/{2,}/g, "/");
  if (isDataRequest(collapsed)
      && (collapsed !== pathname || !isCanonicalDataPath(pathname))) {
    return notCanonical(request, pathname);
  }

  // Заголовки ответа из next() правим на месте. Это разрешено (так же сделано в
  // примере middleware у Cloudflare) и проверено в реальном рантайме через
  // `wrangler pages dev` на всех четырёх видах ответа, которые здесь бывают: статика,
  // JSON функции, 304 без тела и 404.html. Иначе неизменяемые заголовки уронили бы
  // ВЕСЬ сайт, а не одну страницу.
  const response = await next();
  for (const [name, value] of Object.entries(SECURITY_HEADERS)) {
    // Не перезаписываем: функция ставит свои заголовки осознанно (content-type,
    // cache-control, nosniff), и фильтр не должен подменять её решения.
    if (!response.headers.has(name)) response.headers.set(name, value);
  }
  return response;
}
