/**
 * `/data/*` — единственная дверь из браузера в бакет R2.
 *
 * Почему функция, а не публичный адрес бакета: `*.r2.dev` в документации Cloudflare
 * назван путём для разработки и ограничен по частоте, а тот же origin, что и страница,
 * избавляет ещё и от CORS, и от лишнего хоста в CSP. Страница просит `/data/data.json` —
 * и получает ровно тот объект, который положил писатель, без промежуточной публикации.
 *
 * Почему белый список, а не «отдай, что просят». В бакете рядом с витриной живут
 * `raw/{series_id}.json` (сырьё панели, мегабайты точек с 2004 года) и `lease.json`
 * (служебное состояние единственного писателя: кто держит перо и когда бился хартбит).
 * Без списка любой человек с адресом панели читал бы и то, и другое: сырьё — это
 * бессмысленный трафик и операции класса B на каждый запрос, лиз — приглашение
 * подсмотреть, когда VPS замолчал. Проверка идёт по ПОЛНОМУ ключу, а не по префиксу:
 * так ни `..`, ни `%2F`, ни хитрая раскладка сегментов не выведут за пределы трёх
 * разрешённых объектов — что не совпало со строкой из списка, того не существует.
 *
 * Почему честный 404, а не страница. Cloudflare Pages на путь, для которого не нашлось
 * ни функции, ни файла, отвечает **200 и HTML главной страницы**. Для браузера это
 * удобно, для программы — ложь: она просила данные, получила «успех» и разбирает вёрстку
 * как JSON. Соседняя панель заплатила за это дважды за двое суток: движок алертов девять
 * часов не проверял ни одного правила при зелёных прогонах, а сторож свежести объявил
 * три живые панели мёртвыми. Здесь под `/data/` нет ни одного пути, который отвечал бы
 * вёрсткой, — ни на GET, ни на HEAD, ни на POST.
 */

import { bodilessStatus } from "../../lib/conditional-requests.js";

// Витрина, дневная история и история мониторов — всё, что читает фронт. Новый объект
// в бакете попадает к читателю только через эту строку, и это намеренно: список
// пересматривается вместе с `docs/CONTRACT.md` §3, а не по факту появления файла.
const PUBLIC_OBJECTS = new Set([
  "data.json",
  "history/daily.json",
  "history/monitors.json",
]);

function jsonResponse(status, body, extraHeaders = {}) {
  return new Response(JSON.stringify(body), {
    status,
    headers: {
      "content-type": "application/json; charset=utf-8",
      // Витрину переписывает конвейер, и вопрос страницы всегда один: «что сейчас».
      // Кэшировать ответ (в том числе ответ об ошибке) — значит однажды показать
      // вчерашнюю аварию как сегодняшнее состояние.
      "cache-control": "no-store",
      ...extraHeaders,
    },
  });
}

/** Ключ объекта из сегментов пути. `[[path]]` отдаёт массив, `/data/` — пустоту. */
function objectKey(params) {
  const raw = params && params.path;
  if (Array.isArray(raw)) return raw.join("/");
  return typeof raw === "string" ? raw : "";
}

export async function onRequestGet({ env, request, params }) {
  const key = objectKey(params);

  if (!PUBLIC_OBJECTS.has(key)) {
    return jsonResponse(404, {
      error: "no such data object",
      requested: key,
      available: [...PUBLIC_OBJECTS],
      hint: "под /data/ отдаются только объекты витрины; сырьё и лиз наружу не выходят",
    });
  }

  const object = await env.DATA.get(key, { onlyIf: request.headers });

  if (object === null) {
    // 503, а не 404: объект разрешён, но конвейер его ещё ни разу не положил (первый
    // запуск, пересозданный бакет, потерянный лиз). Разница важна для сторожа: 404 —
    // это «такого не бывает, чини адрес», 503 — «писатель молчит, чини писателя».
    // Тело — JSON, потому что страница разбирает ответ как JSON и на HTML-заглушке
    // упала бы с сообщением про синтаксис вместо сообщения про конвейер.
    return jsonResponse(
      503,
      {
        error: "object has not been published to R2 yet",
        object: key,
        hint: "конвейер ещё не публиковал этот объект — смотреть moex-radar-daily на VPS",
      },
      { "retry-after": "300" },
    );
  }

  const headers = new Headers();
  object.writeHttpMetadata(headers);
  headers.set("etag", object.httpEtag);
  headers.set("content-type", "application/json; charset=utf-8");
  headers.set("cache-control", "no-store");
  // Last-Modified пишем руками. `writeHttpMetadata` переносит только httpMetadata
  // объекта, а время выгрузки лежит отдельным полем — и именно оно интересует
  // канарейку: по возрасту публикации она отличает «конвейер работает, данных нет»
  // от «конвейер встал». Без этого заголовка внешний сторож (cron-job.org ходит
  // методом HEAD) вердикта вынести не может вовсе.
  if (object.uploaded) {
    headers.set("last-modified", object.uploaded.toUTCString());
    headers.set("x-data-uploaded", object.uploaded.toISOString());
  }

  if (!("body" in object) || object.body === null) {
    // 304 говорит «твоя копия актуальна». Клиенту, пришедшему с If-Match, эта фраза
    // не подходит: копии у него нет, а условие не выполнено — это 412. См. модуль.
    return new Response(null, { status: bodilessStatus(request, object), headers });
  }
  return new Response(object.body, { headers });
}

// HEAD — это GET без тела, и обслуживать его обязана та же функция. Без этого экспорта
// Pages не находит обработчика на метод и уходит к статике, а та на неизвестный путь
// отвечает 200 и HTML главной страницы: сторож свежести, спрашивающий Last-Modified
// именно методом HEAD, получил бы заглушку и объявил живую панель мёртвой.
export async function onRequestHead(context) {
  const response = await onRequestGet(context);
  // Тело не просто выбрасываем, а закрываем: поток из R2, оставленный висеть, держит
  // соединение до сборки мусора, а HEAD у нас зовут чаще всех остальных методов.
  if (response.body) await response.body.cancel();
  return new Response(null, { status: response.status, headers: response.headers });
}

// Остальные методы. Экспорты под конкретный метод имеют приоритет над `onRequest`,
// поэтому GET и HEAD сюда не попадают — сюда попадает POST/PUT/DELETE, и без этой
// заглушки они утекли бы к статике за тем же ложным «200 и вёрстка». Писать в бакет
// через страницу нельзя в принципе: единственный писатель — конвейер, у него ключи S3.
export function onRequest({ request }) {
  return jsonResponse(
    405,
    { error: "method not allowed", method: request.method },
    { allow: "GET, HEAD" },
  );
}
