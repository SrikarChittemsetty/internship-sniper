"""Tiny stdlib HTTP helper — no external deps, Python 3.9+."""
import gzip
import json
import time
import urllib.request
import urllib.error

UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0 Safari/537.36"
)

DEFAULT_TIMEOUT = 25


def _open(req, timeout):
    return urllib.request.urlopen(req, timeout=timeout)


def fetch(url, method="GET", body=None, headers=None, timeout=DEFAULT_TIMEOUT, retries=2):
    """Fetch a URL, return (status, bytes). Retries on transient errors."""
    hdrs = {
        "User-Agent": UA,
        "Accept": "application/json, text/plain, */*",
        "Accept-Encoding": "gzip",
    }
    if headers:
        hdrs.update(headers)
    data = None
    if body is not None:
        data = body if isinstance(body, bytes) else json.dumps(body).encode()
        hdrs.setdefault("Content-Type", "application/json")

    last_err = None
    for attempt in range(retries + 1):
        try:
            req = urllib.request.Request(url, data=data, headers=hdrs, method=method)
            with _open(req, timeout) as resp:
                raw = resp.read()
                if resp.headers.get("Content-Encoding") == "gzip":
                    raw = gzip.decompress(raw)
                return resp.status, raw
        except urllib.error.HTTPError as e:
            # 4xx: no point retrying (except 429)
            if e.code == 429 and attempt < retries:
                time.sleep(2 * (attempt + 1))
                last_err = e
                continue
            return e.code, e.read() if e.fp else b""
        except Exception as e:  # URLError, timeout, ConnectionReset...
            last_err = e
            if attempt < retries:
                time.sleep(1.5 * (attempt + 1))
    raise RuntimeError("fetch failed: %s (%s)" % (url, last_err))


def fetch_json(url, **kw):
    """Fetch and parse JSON. Returns None on non-200 or parse failure."""
    try:
        status, raw = fetch(url, **kw)
    except RuntimeError:
        return None
    if status != 200 or not raw:
        return None
    try:
        return json.loads(raw.decode("utf-8", "replace"))
    except ValueError:
        return None
