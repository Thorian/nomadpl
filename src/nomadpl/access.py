"""Static identity allowlists shared by both page protocols."""

from pathlib import Path


def permitted(file, root, identity):
    """Check an optional .allowed sidecar without executing it or reading outside root."""
    sidecar = Path(str(file) + ".allowed")
    if not sidecar.exists() and not sidecar.is_symlink():
        return True
    if identity is None or not sidecar.resolve().is_relative_to(root):
        return False
    try:
        with sidecar.open("rb") as stream:
            data = stream.read(8193)
        if len(data) > 8192:
            return False
        entries = [
            line.strip().lower()
            for line in data.decode("ascii").splitlines()
            if line.strip()
        ]
        if any(
            len(entry) != 32 or any(char not in "0123456789abcdef" for char in entry)
            for entry in entries
        ):
            return False
        return identity.hex() in entries
    except (OSError, UnicodeError):
        return False
