"""Signal plugin interface and registry.

To add a signal: subclass Signal, implement collect(), and decorate with @register. That is all the rest of the
system needs. See _template.py for a starting point, including how a paid feed would slot in.
"""
from dataclasses import dataclass, field

REGISTRY = {}


@dataclass
class Context:
    """What a signal gets to work with on each run."""
    day: str                 # ISO date being processed (UTC)
    cfg: object              # msc.config.Config
    http: object             # msc.http.Http (or a test double with .get())
    store: object            # msc.store.Store, including kv scratch space for the plugin's own history
    prices: object = None    # callable returning [(day, open, close)] for the market anchor, or None


class Signal:
    name = ""
    description = ""
    needs = ()               # names of settings or keys this signal requires, shown on the dashboard

    def collect(self, ctx):
        """Return a list of msc.models.Observation. Raise on failure; the engine records it as unhealthy."""
        raise NotImplementedError


def register(cls):
    if not cls.name:
        raise ValueError(f"{cls.__name__} needs a name")
    REGISTRY[cls.name] = cls
    return cls


def load_signals(cfg):
    """Instantiate the enabled signals (all registered ones unless MSC_SIGNALS narrows the list)."""
    from . import builtin  # noqa: F401  (importing registers the built-in signals)
    import importlib
    for mod in cfg.extra_modules:
        importlib.import_module(mod)
    names = list(cfg.signals) or sorted(REGISTRY)
    unknown = [n for n in names if n not in REGISTRY]
    if unknown:
        raise SystemExit(f"Unknown signals {unknown}; available: {sorted(REGISTRY)}")
    return [REGISTRY[n]() for n in names]
