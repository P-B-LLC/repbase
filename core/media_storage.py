"""Publication storage must never silently rename a reserved cleanup key."""
from django.core.exceptions import SuspiciousFileOperation
from django.core.files.storage import FileSystemStorage, InMemoryStorage


class ReservedNameMixin:
    def get_available_name(self, name, max_length=None):
        # A random server-generated name is reserved in the database before
        # upload. Refuse collisions, including a filesystem create race; do
        # not silently allocate a name the cleanup record does not know.
        if max_length is not None and len(name) > max_length:
            raise SuspiciousFileOperation('Reserved media name is too long.')
        if self.exists(name):
            raise FileExistsError('Reserved media name already exists.')
        return name

    def get_alternative_name(self, file_root, file_ext):
        raise FileExistsError('Reserved media names cannot be changed.')

    def save_reserved(self, name, content):
        """Write exactly this key or fail. Custom object stores need this contract."""
        return self.save(name, content)


class ReservedNameFileSystemStorage(ReservedNameMixin, FileSystemStorage):
    pass


class ReservedNameInMemoryStorage(ReservedNameMixin, InMemoryStorage):
    """Same name contract for isolated tests; never used for persisted data."""
    pass
