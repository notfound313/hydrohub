from dataclasses import dataclass
from email.utils import parseaddr
from importlib.metadata import metadata


@dataclass(frozen=True)
class AppMetadata:
    name: str
    version: str
    author: str
    description: str


def get_app_metadata() -> AppMetadata:
    meta = metadata("hydrohub")

    author_email = meta.get("Author-email") or ""
    author = parseaddr(author_email)[0] or "Unknown"

    return AppMetadata(
        name=meta.get("Name") or "HydroHub",
        version=meta.get("Version") or "Unknown",
        author=author,
        description=meta.get("Summary") or "",
    )
