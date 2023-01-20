"""Helper class used to store data in files."""
from __future__ import annotations

import json
import logging
import os
from tempfile import NamedTemporaryFile
from threading import Lock
from typing import TYPE_CHECKING, Any

from viseron.const import STORAGE_PATH
from viseron.exceptions import ViseronError
from viseron.helpers.json import JSONEncoder

if TYPE_CHECKING:
    from viseron import Viseron

LOGGER = logging.getLogger(__name__)


class StorageWriteError(ViseronError):
    """Error writing storage data to file."""


class StorageReadError(ViseronError):
    """Error reading storage data from file."""


class Storage:
    """Class used to store JSON data in files."""

    def __init__(self, vis: Viseron, key: str, version=1) -> None:
        """Initialize the storage class."""
        self._vis = vis
        self.key = key
        self.version = version
        self._lock = Lock()
        self._data: dict[str, Any] | None = None

    @property
    def data(self) -> dict[str, Any]:
        """Return data."""
        with self._lock:
            if self._data is None:
                self._data = self._load()
        return self._data["data"]

    def _write(self, data: str) -> None:
        """Write data to file."""
        LOGGER.debug("Writing data to %s", self.path)
        os.makedirs(os.path.dirname(self.path), exist_ok=True)

        temp_file = ""
        tmp_path = os.path.split(self.path)[0]
        try:
            with NamedTemporaryFile(
                mode="w", encoding="utf-8", dir=tmp_path, delete=False
            ) as file:
                file.write(data)
                temp_file = file.name
            os.replace(temp_file, self.path)
        except OSError as error:
            LOGGER.error("Error writing to file %s: %s", self.path, error)
            raise StorageWriteError from error
        finally:
            # Remove temp file if it exists
            if os.path.exists(temp_file):
                try:
                    os.remove(temp_file)
                except OSError as err:
                    LOGGER.error(
                        "Failed to delete tempfile %s while saving %s: %s",
                        temp_file,
                        self.path,
                        err,
                    )

    def save(self, data: dict) -> None:
        """Write data to file."""
        self._data = {
            "version": self.version,
            "data": data,
        }
        json_data = json.dumps(self._data, indent=4, cls=JSONEncoder)
        with self._lock:
            self._write(json_data)

    def _load(self):
        """Load data from file."""
        data = {}
        try:
            with open(self.path, encoding="utf-8") as file:
                data = json.load(file)
                if data["version"] != self.version:
                    LOGGER.warning(
                        "Storage version mismatch for %s. Expected %s, got %s",
                        self.key,
                        self.version,
                        data["version"],
                    )
        except FileNotFoundError:
            LOGGER.debug("Storage file not found: %s", self.path)
        except OSError as error:
            LOGGER.error("Error reading from file %s: %s", self.path, error)
            raise StorageReadError from error
        return data

    @property
    def path(self) -> str:
        """Return storage path."""
        return os.path.join(STORAGE_PATH, self.key)
