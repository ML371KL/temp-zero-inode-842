"""Cloudflare R2 через S3-совместимый API без boto3: SigV4 подписывается руками.

ПОЧЕМУ не boto3: пайплайн обязан подниматься на голой машине без venv
(docs/CONTRACT.md §0), а boto3 — десятки мегабайт зависимостей ради четырёх
HTTP-запросов. Подпись SigV4 — это четыре HMAC-SHA256, стандартная библиотека
справляется полностью.

ПОЧЕМУ обратная вычитка после PUT: в соседнем проекте (838/839, тот же R2) PUT
отвечал 200, а объект в бакете не менялся — сеть/прокси съедали тело. Молчаливый
провал ловится ТОЛЬКО сверкой размера и хэша после записи, поэтому put() по
умолчанию перечитывает то, что записал, и падает громко.
"""

import hashlib
import hmac
import http.client
import json
import os
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone


class R2Error(RuntimeError):
    """Любая проблема с бакетом: конфиг, сеть, статус, расхождение при вычитке."""


_REGION = "auto"          # R2 игнорирует регион, но SigV4 требует непустой
_SERVICE = "s3"
_ALGO = "AWS4-HMAC-SHA256"
_TIMEOUT = 30
_RETRIES = 3
_UA = "moex-radar/1.0 (842 pipeline)"


def config():
    """Конфиг из окружения или None.

    Отсутствие ключей — НЕ ошибка: --dry-run, selftest и локальная отладка обязаны
    работать без бакета. Ошибку поднимает только тот, кто реально идёт в сеть.
    """
    acc = (os.environ.get("R2_ACCOUNT_ID") or "").strip()
    kid = (os.environ.get("R2_ACCESS_KEY_ID") or "").strip()
    sec = (os.environ.get("R2_SECRET_ACCESS_KEY") or "").strip()
    bucket = (os.environ.get("R2_BUCKET") or "").strip()
    if not (acc and kid and sec and bucket):
        return None
    host = (os.environ.get("R2_ENDPOINT_HOST") or "").strip() or f"{acc}.r2.cloudflarestorage.com"
    return {"account": acc, "key_id": kid, "secret": sec, "bucket": bucket, "host": host}


def configured():
    return config() is not None


# --------------------------------------------------------------- подпись SigV4

def _hmac(key, msg):
    return hmac.new(key, msg.encode("utf-8"), hashlib.sha256).digest()


def _canonical_path(cfg, key):
    # '/' обязан остаться разделителем сегментов, иначе подпись не сойдётся с тем,
    # что видит сервер: квотим всё, кроме '/' и '~'.
    return "/" + urllib.parse.quote(f"{cfg['bucket']}/{key.lstrip('/')}", safe="/~")


def _sign(cfg, method, path, payload_hash, headers, now):
    amz_date = now.strftime("%Y%m%dT%H%M%SZ")
    datestamp = amz_date[:8]
    h = {k.lower(): str(v).strip() for k, v in (headers or {}).items()}
    h["host"] = cfg["host"]
    h["x-amz-date"] = amz_date
    h["x-amz-content-sha256"] = payload_hash

    signed = sorted(h)
    canon_headers = "".join(f"{k}:{h[k]}\n" for k in signed)
    canon_req = "\n".join([method, path, "", canon_headers, ";".join(signed), payload_hash])
    scope = f"{datestamp}/{_REGION}/{_SERVICE}/aws4_request"
    sts = "\n".join([_ALGO, amz_date, scope,
                     hashlib.sha256(canon_req.encode("utf-8")).hexdigest()])

    k_date = _hmac(("AWS4" + cfg["secret"]).encode("utf-8"), datestamp)
    k_region = _hmac(k_date, _REGION)
    k_service = _hmac(k_region, _SERVICE)
    k_signing = _hmac(k_service, "aws4_request")
    signature = hmac.new(k_signing, sts.encode("utf-8"), hashlib.sha256).hexdigest()

    out = dict(h)
    out["authorization"] = (f"{_ALGO} Credential={cfg['key_id']}/{scope}, "
                            f"SignedHeaders={';'.join(signed)}, Signature={signature}")
    return out


def _request(method, key, body=b"", headers=None, timeout=_TIMEOUT, retries=_RETRIES):
    cfg = config()
    if cfg is None:
        raise R2Error("R2 не сконфигурирован: нужны R2_ACCOUNT_ID, R2_ACCESS_KEY_ID, "
                      "R2_SECRET_ACCESS_KEY, R2_BUCKET")
    path = _canonical_path(cfg, key)
    url = f"https://{cfg['host']}{path}"
    payload_hash = hashlib.sha256(body or b"").hexdigest()
    last = None
    for attempt in range(1, retries + 1):
        # Подпись пересчитывается на КАЖДОЙ попытке: x-amz-date входит в неё, а
        # просроченная на 15 минут подпись даёт 403, который выглядит как «нет прав».
        signed = _sign(cfg, method, path, payload_hash, headers, datetime.now(timezone.utc))
        req = urllib.request.Request(url, data=(body if method in ("PUT", "POST") else None),
                                     method=method)
        for k, v in signed.items():
            req.add_header(k, v)
        req.add_header("User-Agent", _UA)
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return resp.status, {k.lower(): v for k, v in resp.headers.items()}, resp.read()
        except urllib.error.HTTPError as exc:
            hdrs = {k.lower(): v for k, v in (exc.headers or {}).items()}
            try:
                data = exc.read()
            except (OSError, http.client.HTTPException):
                # Исключение, поднятое ВНУТРИ обработчика, соседними except уже не
                # ловится: обрыв тела на чтении текста ошибки улетал наружу мимо всех
                # ретраев. Тело ошибки не стоит падения прогона — читаем как пустое.
                data = b""
            if exc.code in (404, 412):
                return exc.code, hdrs, data
            if exc.code < 500 and exc.code != 429:
                # 4xx не лечится ретраем: ключ/подпись/имя объекта не изменятся.
                raise R2Error(f"{method} {key}: HTTP {exc.code} "
                              f"{data[:300].decode('utf-8', 'replace')}") from exc
            last = f"HTTP {exc.code}"
        except (urllib.error.URLError, TimeoutError, OSError,
                http.client.HTTPException) as exc:
            # http.client.HTTPException (IncompleteRead, BadStatusLine, LineTooLong)
            # — НЕ подкласс OSError, и без него обрыв тела ответа улетал наружу мимо
            # ретраев: на PUT это трейсбек из publish() (который обещает исключений
            # не выпускать), на GET lease.json — тихий no-op с ok=True и кодом 0, то
            # есть VPS не написал ничего, а прогон отчитался успехом. Соседний
            # lib/http.py этот класс в ретраях держит с самого начала — здесь его
            # просто забыли.
            last = f"{type(exc).__name__}: {exc}"
        if attempt < retries:
            time.sleep(1.0 * attempt)
    raise R2Error(f"{method} {key}: не удалось за {retries} попыток ({last})")


# ------------------------------------------------------------------- операции

def head(key):
    """Метаданные объекта или None, если его нет."""
    status, hdrs, _ = _request("HEAD", key)
    if status == 404:
        return None
    return {"size": int(hdrs.get("content-length") or 0),
            "etag": (hdrs.get("etag") or "").strip('"'),
            "last_modified": hdrs.get("last-modified")}


def get(key):
    status, _, body = _request("GET", key)
    return None if status == 404 else body


def put(key, data, content_type="application/octet-stream", cache_control=None, verify=True):
    if isinstance(data, str):
        data = data.encode("utf-8")
    headers = {"content-type": content_type}
    if cache_control:
        headers["cache-control"] = cache_control
    status, hdrs, _ = _request("PUT", key, data, headers)
    if status not in (200, 201, 204):
        raise R2Error(f"PUT {key}: неожиданный статус {status}")
    res = {"key": key, "size": len(data), "etag": (hdrs.get("etag") or "").strip('"')}
    res["verified"] = _verify(key, data) if verify else None
    return res


def _verify(key, data):
    """Обратная вычитка: 200 на PUT ещё не значит, что объект лёг в бакет."""
    meta = head(key)
    if meta is None:
        raise R2Error(f"PUT {key}: объект не читается сразу после записи")
    if meta["size"] != len(data):
        raise R2Error(f"PUT {key}: размер после записи {meta['size']} вместо {len(data)}")
    md5 = hashlib.md5(data, usedforsecurity=False).hexdigest()
    if meta["etag"] and meta["etag"] == md5:
        return "etag"
    # ETag ≠ MD5 бывает при multipart и при шифровании на стороне бакета —
    # тогда единственная честная сверка это перечитать тело целиком.
    body = get(key)
    if body is None or hashlib.sha256(body).digest() != hashlib.sha256(data).digest():
        raise R2Error(f"PUT {key}: содержимое после записи не совпало с отправленным")
    return "sha256"


def put_json(key, obj, cache_control="public, max-age=60", verify=True):
    data = json.dumps(obj, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    return put(key, data, "application/json; charset=utf-8", cache_control, verify)


def get_json(key):
    body = get(key)
    if body is None:
        return None
    try:
        return json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, ValueError) as exc:
        raise R2Error(f"GET {key}: битый JSON ({exc})") from exc
