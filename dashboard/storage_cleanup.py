from __future__ import annotations

from collections.abc import Callable

from django.core.files.storage import Storage
from django.db import transaction


class StoredObjectWrite:
    """Track the exact object created by one storage.save() call.

    Storage is not transactional.  The caller must create this guard from the
    *returned* name (which may differ from the requested name), register
    ``retain_on_commit`` in the database transaction, and call
    ``cleanup_after_rollback`` after that transaction has rolled back.
    """

    def __init__(
        self,
        *,
        storage: Storage,
        stored_name: str,
        is_referenced: Callable[[str], bool],
    ) -> None:
        self.storage = storage
        self.stored_name = stored_name
        self.is_referenced = is_referenced
        self._retained = False

    def retain_on_commit(self) -> None:
        transaction.on_commit(self._mark_retained)

    def _mark_retained(self) -> None:
        self._retained = True

    def cleanup_after_rollback(self) -> None:
        if self._retained or not self.stored_name:
            return
        try:
            # A committed immutable fact always wins over cleanup.  This also
            # protects an object if a storage backend unexpectedly returned a
            # name already adopted by another successful request.
            if self.is_referenced(self.stored_name):
                return
            if self.storage.exists(self.stored_name):
                self.storage.delete(self.stored_name)
        except Exception:
            # The database transaction has already rolled back.  A storage
            # lifecycle job may remove an orphan later; never turn a cleanup
            # failure into a fabricated successful command.
            pass
