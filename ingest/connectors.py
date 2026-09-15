"""Where a firm's file comes from.

Every firm drops files differently and none of them will change how they do it
for us, so transport is a class rather than a branch in the pipeline. The
interface is deliberately small -- list what is there, fetch one, mark it done --
because that is all the worker needs and anything more becomes per-firm code.

Heavy dependencies (paramiko, boto3) are imported lazily so a deployment that
only uses one transport need not install the others.
"""

from __future__ import annotations

import shutil
from abc import ABC, abstractmethod
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path


@dataclass(frozen=True)
class RemoteFile:
    name: str
    size: int
    modified: datetime

    @property
    def stem(self) -> str:
        return Path(self.name).stem


class Connector(ABC):
    """A place files arrive from."""

    kind: str = "abstract"

    @abstractmethod
    def list(self) -> list[RemoteFile]:
        """Files currently waiting, oldest first."""

    @abstractmethod
    def fetch(self, name: str, destination: Path) -> Path:
        """Copy one file locally and return where it landed."""

    def complete(self, name: str) -> None:
        """Called once a file has been processed. Default is to leave it alone.

        Not deleting by default is deliberate: a firm's drop directory is often
        also their own record of what they sent.
        """

    def describe(self) -> dict:
        return {"kind": self.kind}


class LocalConnector(Connector):
    """A directory on disk. Used for development, and for firms who push to us."""

    kind = "local"

    def __init__(self, directory: Path | str, pattern: str = "*", archive: bool = True) -> None:
        self.directory = Path(directory)
        self.directory.mkdir(parents=True, exist_ok=True)
        self.pattern = pattern
        self.archive = archive

    def list(self) -> list[RemoteFile]:
        files = [
            RemoteFile(
                p.name,
                p.stat().st_size,
                datetime.fromtimestamp(p.stat().st_mtime, tz=timezone.utc),
            )
            for p in self.directory.glob(self.pattern)
            if p.is_file()
        ]
        return sorted(files, key=lambda f: f.modified)

    def fetch(self, name: str, destination: Path) -> Path:
        source = self.directory / name
        if not source.is_file():
            raise FileNotFoundError(name)
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, destination)
        return destination

    def complete(self, name: str) -> None:
        if not self.archive:
            return
        done = self.directory / "_processed"
        done.mkdir(exist_ok=True)
        source = self.directory / name
        if source.is_file():
            shutil.move(str(source), str(done / f"{datetime.now():%Y%m%dT%H%M%S}-{name}"))

    def describe(self) -> dict:
        return {"kind": self.kind, "directory": str(self.directory), "pattern": self.pattern}


class S3Connector(Connector):
    """An S3 prefix, either ours or the firm's own bucket."""

    kind = "s3"

    def __init__(self, bucket: str, prefix: str = "", suffix: str = "", **client_kwargs) -> None:
        self.bucket = bucket
        self.prefix = prefix.lstrip("/")
        self.suffix = suffix
        self._client_kwargs = client_kwargs
        self._client = None

    @property
    def client(self):
        if self._client is None:
            import boto3

            self._client = boto3.client("s3", **self._client_kwargs)
        return self._client

    def list(self) -> list[RemoteFile]:
        paginator = self.client.get_paginator("list_objects_v2")
        out: list[RemoteFile] = []
        for page in paginator.paginate(Bucket=self.bucket, Prefix=self.prefix):
            for obj in page.get("Contents", []):
                key = obj["Key"]
                if key.endswith("/") or (self.suffix and not key.endswith(self.suffix)):
                    continue
                out.append(RemoteFile(key, obj["Size"], obj["LastModified"]))
        return sorted(out, key=lambda f: f.modified)

    def fetch(self, name: str, destination: Path) -> Path:
        destination.parent.mkdir(parents=True, exist_ok=True)
        self.client.download_file(self.bucket, name, str(destination))
        return destination

    def describe(self) -> dict:
        return {"kind": self.kind, "bucket": self.bucket, "prefix": self.prefix}


class SFTPConnector(Connector):
    """A directory on the firm's SFTP server. Still the most common transport.

    Credentials come from the caller rather than being stored here; the control
    plane holds a secret reference, not the secret.
    """

    kind = "sftp"

    def __init__(
        self,
        host: str,
        username: str,
        directory: str = ".",
        pattern: str = "*",
        port: int = 22,
        password: str | None = None,
        key_path: str | None = None,
    ) -> None:
        self.host = host
        self.port = port
        self.username = username
        self.directory = directory
        self.pattern = pattern
        self._password = password
        self._key_path = key_path

    def _connect(self):
        import paramiko

        client = paramiko.SSHClient()
        client.load_system_host_keys()
        # Refuse unknown hosts rather than trusting on first use: this is a
        # credentialled connection to a customer's infrastructure.
        client.set_missing_host_key_policy(paramiko.RejectPolicy())
        client.connect(
            self.host, port=self.port, username=self.username,
            password=self._password, key_filename=self._key_path,
        )
        return client, client.open_sftp()

    def list(self) -> list[RemoteFile]:
        import fnmatch

        client, sftp = self._connect()
        try:
            out = [
                RemoteFile(
                    entry.filename,
                    entry.st_size or 0,
                    datetime.fromtimestamp(entry.st_mtime or 0, tz=timezone.utc),
                )
                for entry in sftp.listdir_attr(self.directory)
                if fnmatch.fnmatch(entry.filename, self.pattern)
            ]
            return sorted(out, key=lambda f: f.modified)
        finally:
            sftp.close()
            client.close()

    def fetch(self, name: str, destination: Path) -> Path:
        client, sftp = self._connect()
        try:
            destination.parent.mkdir(parents=True, exist_ok=True)
            sftp.get(f"{self.directory}/{name}", str(destination))
            return destination
        finally:
            sftp.close()
            client.close()

    def describe(self) -> dict:
        return {
            "kind": self.kind, "host": self.host, "port": self.port,
            "username": self.username, "directory": self.directory,
        }


CONNECTORS: dict[str, type[Connector]] = {
    "local": LocalConnector,
    "s3": S3Connector,
    "sftp": SFTPConnector,
}


def build_connector(kind: str, settings: dict) -> Connector:
    """Instantiate a connector from stored settings."""
    if kind not in CONNECTORS:
        raise ValueError(f"unknown connector kind: {kind} (have {sorted(CONNECTORS)})")
    return CONNECTORS[kind](**settings)


def describe_connectors() -> list[dict]:
    """Catalogue for the admin UI."""
    return [
        {"kind": "local", "settings": ["directory", "pattern", "archive"],
         "description": "A directory we watch; also where HTTPS pushes land"},
        {"kind": "s3", "settings": ["bucket", "prefix", "suffix"],
         "description": "An S3 prefix, ours or the firm's"},
        {"kind": "sftp", "settings": ["host", "port", "username", "directory", "pattern", "key_path"],
         "description": "The firm's SFTP server; host key must be known"},
    ]
