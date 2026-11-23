"""Tiny rate-limited HTTP GET client on the standard library (no third-party packages)."""
import time
import urllib.error
import urllib.parse
import urllib.request


class HttpError(RuntimeError):
    def __init__(self, status, message):
        super().__init__(message)
        self.status = status


class Http:
    def __init__(self, user_agent="MarketSignalConductor/0.1", min_interval=0.2, sleep=time.sleep, opener=None):
        self.user_agent, self.min_interval, self._sleep = user_agent, min_interval, sleep
        self._open = opener or urllib.request.urlopen
        self._last = 0.0

    def get(self, url, params=None, headers=None, retries=3, timeout=45):
        if params:
            url += ("&" if "?" in url else "?") + urllib.parse.urlencode(params)
        hdrs = {"User-Agent": self.user_agent, "Accept": "*/*"}
        hdrs.update(headers or {})
        for attempt in range(retries):
            wait = self.min_interval - (time.monotonic() - self._last)
            if wait > 0:
                self._sleep(wait)
            self._last = time.monotonic()
            try:
                with self._open(urllib.request.Request(url, headers=hdrs), timeout=timeout) as resp:
                    return resp.read().decode("utf-8", errors="replace")
            except urllib.error.HTTPError as exc:
                if exc.code in (429, 500, 502, 503, 504) and attempt < retries - 1:
                    self._sleep(2 ** attempt * 2)
                    continue
                raise HttpError(exc.code, f"HTTP {exc.code} for {url}") from exc
            except (urllib.error.URLError, TimeoutError, OSError) as exc:
                if attempt < retries - 1:
                    self._sleep(2 ** attempt)
                    continue
                raise HttpError(0, f"network error for {url}: {exc}") from exc
        raise HttpError(0, f"giving up on {url}")
