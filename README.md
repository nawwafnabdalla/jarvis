# jarvis

Local-only GBPUSD research and backtesting toolkit. Windows-native, Python 3.12.

No server, no container, no web framework, no broker API, no ML. See the
Product Bible and Technical Spec for the full design.

## Setup

```
py -3.12 -m venv .venv
.venv\Scripts\activate
pip install -e ".[dev]"
```

## Commands

```
pytest tests/ -v
lint-imports
jarvis doctor
jarvis --help
```

## Layout

- `src/jarvis/core/` — identifiers, canonical serialisation, hashing, error
  hierarchy, configuration loading. The only module every other layer may
  depend on.
- `src/jarvis/*/` — one package per pipeline stage (see `.importlinter` for
  the enforced layer order). Every module beyond `core` and `cli` is an
  empty skeleton until its own work package lands.
- `config/` — frozen instrument and period definitions.
- `tests/unit/` — unit tests for `core`.
- `tests/architecture/` — import-linter and module-skeleton enforcement.
