"""HTTP-Zugriff mit Abfrage-Limits pro Host, Wiederholungen und Datei-Cache."""

from __future__ import annotations

import hashlib
import logging
import threading
import time
from pathlib import Path
from urllib.parse import urlparse

import requests

log = logging.getLogger(__name__)

RETRY_STATUS = {429, 500, 502, 503, 504}


class HttpError(RuntimeError):
    pass


def _describe(exc: Exception, host: str) -> str:
    text = str(exc)
    if isinstance(exc, requests.Timeout):
        return f"Keine Verbindung zu {host} (Zeitüberschreitung)"
    for code in ("403", "407", "502"):
        if f"{code} " in text or f"response {code}" in text:
            return f"Keine Verbindung zu {host} (Proxy/Firewall antwortet {code})"
    return f"Keine Verbindung zu {host} ({type(exc).__name__})"


class HttpClient:
    """Einfacher HTTP-Client, der pro Host einen Mindestabstand zwischen Anfragen einhält.

    ``rate_limits`` ordnet einem Hostnamen (oder einer Endung wie ``sec.gov``) die
    Mindestpause in Sekunden zu. Antworten mit 429/5xx werden mit wachsender Pause
    wiederholt; ein ``Retry-After``-Header wird beachtet.
    """

    def __init__(
        self,
        user_agent: str,
        rate_limits: dict[str, float] | None = None,
        default_interval: float = 1.0,
        timeout: float = 20.0,
        retries: int = 3,
        cache_dir: str | Path | None = None,
    ):
        self.session = requests.Session()
        self.session.headers["User-Agent"] = user_agent
        self.rate_limits = rate_limits or {}
        self.default_interval = default_interval
        self.timeout = timeout
        self.retries = retries
        self.cache_dir = Path(cache_dir) if cache_dir else None
        self._last: dict[str, float] = {}
        self._dead_hosts: dict[str, str] = {}
        self._lock = threading.Lock()

    def _interval(self, host: str) -> float:
        for key, value in self.rate_limits.items():
            if host == key or host.endswith("." + key):
                return float(value)
        return self.default_interval

    def _throttle(self, host: str) -> None:
        with self._lock:
            wait = self._last.get(host, 0.0) + self._interval(host) - time.monotonic()
            if wait > 0:
                time.sleep(wait)
            self._last[host] = time.monotonic()

    def request(self, method: str, url: str, **kwargs) -> requests.Response:
        host = urlparse(url).hostname or ""
        if host in self._dead_hosts:
            # Host war in diesem Lauf schon nicht erreichbar – nicht erneut minutenlang warten.
            raise HttpError(self._dead_hosts[host])
        kwargs.setdefault("timeout", self.timeout)
        last_error: Exception | None = None
        for attempt in range(self.retries + 1):
            self._throttle(host)
            try:
                resp = self.session.request(method, url, **kwargs)
            except (requests.ConnectionError, requests.Timeout) as exc:
                last_error = HttpError(_describe(exc, host))
            except requests.RequestException as exc:
                last_error = HttpError(f"{type(exc).__name__} bei {host}")
                break
            else:
                if resp.status_code < 400:
                    return resp
                last_error = HttpError(f"HTTP {resp.status_code} für {url}")
                if resp.status_code not in RETRY_STATUS:
                    break
                retry_after = resp.headers.get("Retry-After", "")
                if retry_after.isdigit():
                    time.sleep(min(int(retry_after), 60))
            if attempt < self.retries:
                time.sleep(2 ** attempt)
        if isinstance(last_error, HttpError) and str(last_error).startswith("Keine Verbindung"):
            self._dead_hosts[host] = str(last_error)
        raise HttpError(str(last_error))

    def get(self, url: str, **kwargs) -> requests.Response:
        return self.request("GET", url, **kwargs)

    def post(self, url: str, **kwargs) -> requests.Response:
        return self.request("POST", url, **kwargs)

    def get_text(self, url: str, cache_hours: float = 0, **kwargs) -> str:
        cached = self._cache_read(url, cache_hours)
        if cached is not None:
            return cached
        resp = self.get(url, **kwargs)
        text = resp.text
        self._cache_write(url, text, cache_hours)
        return text

    def get_json(self, url: str, cache_hours: float = 0, **kwargs):
        import json

        return json.loads(self.get_text(url, cache_hours=cache_hours, **kwargs))

    def _cache_path(self, url: str) -> Path | None:
        if not self.cache_dir:
            return None
        return self.cache_dir / (hashlib.sha1(url.encode()).hexdigest() + ".cache")

    def _cache_read(self, url: str, hours: float) -> str | None:
        path = self._cache_path(url)
        if not hours or not path or not path.exists():
            return None
        if time.time() - path.stat().st_mtime > hours * 3600:
            return None
        return path.read_text(encoding="utf-8")

    def _cache_write(self, url: str, text: str, hours: float) -> None:
        path = self._cache_path(url)
        if hours and path:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(text, encoding="utf-8")
