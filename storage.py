import json
import logging
import os
from pathlib import Path

BOT_ROOT = Path(__file__).resolve().parent

log = logging.getLogger(__name__)

_data_dir = BOT_ROOT / "data"


def configure(path):
    global _data_dir
    _data_dir = Path(path).resolve()


def data_dir():
    return _data_dir


class Store:
    def __init__(self, filename, default=dict):
        self.name = filename
        self._default = default

    @property
    def path(self):
        return _data_dir / self.name

    @property
    def backup(self):
        return _data_dir / (self.name + ".bak")

    @property
    def legacy(self):
        return BOT_ROOT / self.name

    def load(self):
        for candidate in (self.path, self.backup, self.legacy):
            if not candidate.exists():
                continue
            try:
                with open(candidate, "r", encoding="utf-8") as f:
                    data = json.load(f)
                if candidate != self.path:
                    log.warning(
                        "recovered %s from %s", self.name, candidate.name
                    )
                    self._write(data, rotate=False)
                return data
            except (ValueError, OSError) as exc:
                log.warning("%s unreadable: %s", candidate.name, exc)

        return self._default()

    def save(self, data):
        return self._write(data, rotate=True)

    def _write(self, data, rotate):
        try:
            _data_dir.mkdir(parents=True, exist_ok=True)
            tmp = _data_dir / (self.name + ".tmp")

            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(data, f, indent=2)
                f.flush()
                os.fsync(f.fileno())

            if rotate and self.path.exists():
                os.replace(self.path, self.backup)

            os.replace(tmp, self.path)
            return True
        except OSError as exc:
            log.error("failed to write %s: %s", self.name, exc)
            return False


class IntKeyStore(Store):
    def load(self):
        raw = super().load()
        if not isinstance(raw, dict):
            return self._default()
        try:
            return {int(k): v for k, v in raw.items()}
        except (ValueError, TypeError):
            log.warning("%s has non integer keys, discarding", self.name)
            return self._default()

    def _write(self, data, rotate):
        return super()._write({str(k): v for k, v in data.items()}, rotate)