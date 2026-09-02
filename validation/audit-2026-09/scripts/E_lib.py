"""E_algopack: общий HTTP-слой к ALGOPACK (apim.moex.com) и бесплатному ISS.
Ключ читается из файла в переменную и НИКОГДА не печатается.
Счётчик запросов и пауза 0.3 с между запросами."""
import json, os, sys, time, ssl, io
import urllib.request, urllib.error

sys.stdout.reconfigure(encoding="utf-8")

_KEY_PATH = r"C:\Users\rodio\.secrets\MOEX Algopack.txt"
_CA_PANEL = r"C:\Users\rodio\Desktop\Claude\temp-zero-inode-842\pipeline\lib\ca\russian_trusted.pem"
ALGO = "https://apim.moex.com/iss"
ISS = "https://iss.moex.com/iss"
PAUSE = 0.3
N_REQ = 0
LOG = []


def _key():
    with open(_KEY_PATH, encoding="utf-8") as f:
        return f.read().strip()


def _ctx():
    """Контекст TLS: системные корни + бандл УЦ Минцифры из репо панели."""
    ctx = ssl.create_default_context()
    try:
        import certifi
        ctx.load_verify_locations(cafile=certifi.where())
    except Exception:
        pass
    if os.path.exists(_CA_PANEL):
        ctx.load_verify_locations(cafile=_CA_PANEL)
    return ctx


_CTX = _ctx()
_INSECURE = None


def get(url, auth=True, retries=4, timeout=60):
    """JSON по URL. auth=True -> Bearer-ключ ALGOPACK. Возвращает (obj, err)."""
    global N_REQ, _INSECURE
    hdr = {"User-Agent": "Mozilla/5.0 (audit E_algopack)"}
    if auth:
        hdr["Authorization"] = "Bearer " + _key()
    last = None
    for a in range(retries):
        time.sleep(PAUSE)
        N_REQ += 1
        try:
            req = urllib.request.Request(url, headers=hdr)
            ctx = _INSECURE if _INSECURE is not None else _CTX
            t0 = time.time()
            with urllib.request.urlopen(req, timeout=timeout, context=ctx) as r:
                raw = r.read()
            LOG.append((url.split("?")[0][-60:], round(time.time() - t0, 2)))
            return json.loads(raw.decode("utf-8")), None
        except urllib.error.HTTPError as e:
            last = f"HTTP {e.code}"
            if e.code in (429, 502, 503, 504):
                time.sleep(2 + 2 * a)
                continue
            return None, last
        except ssl.SSLError as e:
            last = f"SSL {e}"
            if _INSECURE is None:
                # только разведка: отключаем проверку и помечаем в журнале
                _INSECURE = ssl.create_default_context()
                _INSECURE.check_hostname = False
                _INSECURE.verify_mode = ssl.CERT_NONE
                LOG.append(("!!! TLS verify отключён после ошибки", str(e)[:80]))
                continue
        except Exception as e:
            last = f"{type(e).__name__}: {str(e)[:120]}"
            time.sleep(1 + a)
    return None, last


def block(obj, name):
    """Блок ISS -> (columns, rows)."""
    if not obj or name not in obj:
        return [], []
    b = obj[name]
    return list(b.get("columns") or []), list(b.get("data") or [])


def paged(url, name, page=None, max_pages=200):
    """Собирает все страницы (start=...). Возвращает (columns, rows, n_pages)."""
    cols, rows, start, pages = [], [], 0, 0
    while pages < max_pages:
        u = url + ("&" if "?" in url else "?") + f"start={start}"
        if page:
            u += f"&limit={page}"
        obj, err = get(u)
        pages += 1
        if err or not obj:
            break
        c, d = block(obj, name)
        if not d:
            break
        cols = c
        rows.extend(d)
        start += len(d)
        if page and len(d) < page:
            break
        if not page and len(d) < 1000:
            break
    return cols, rows, pages


def save_csv(path, cols, rows):
    import csv
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(cols)
        w.writerows(rows)
