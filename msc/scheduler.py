"""In-process daily scheduler: one always-on process runs the web dashboard and the daily job."""
import logging
import threading
import time
from datetime import datetime, timezone

log = logging.getLogger("msc.scheduler")


def is_due(now, run_hour, run_minute, last_run_day):
    """True once per UTC day, at or after the run time. A restart after the run time catches up the missed run."""
    today = now.date().isoformat()
    return last_run_day != today and (now.hour, now.minute) >= (run_hour, run_minute)


def loop(conductor, cfg, run_lock, stop, now_fn=lambda: datetime.now(timezone.utc), poll=30):
    while not stop.is_set():
        last = conductor.store.kv_get("last_run_day")
        if is_due(now_fn(), cfg.run_hour_utc, cfg.run_minute_utc, last):
            if run_lock.acquire(blocking=False):
                try:
                    result = conductor.run_day()
                    conductor.store.kv_set("last_run_day", result["day"])
                    log.info("daily run done: regime=%s p_up=%.3f", result["regime"], result["p_up"])
                except Exception:
                    log.exception("daily run failed; will retry on the next poll")
                    time.sleep(300)
                finally:
                    run_lock.release()
        stop.wait(poll)


def start(conductor, cfg, run_lock):
    stop = threading.Event()
    t = threading.Thread(target=loop, args=(conductor, cfg, run_lock, stop), daemon=True, name="msc-scheduler")
    t.start()
    return stop
