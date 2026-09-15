"""Batch artifacts on disk or in object storage.

A batch is written once by a build worker and read many times by query
services, so the artifacts are shaped around that asymmetry:

  positions.parquet   the raw book -- everything needed to reprice. 9x smaller
                      than the full batch, so it is what cold retention keeps.
  batch.parquet       positions + greeks + the scenario matrix. What the grid
                      reads. Scanned lazily rather than loaded, because
                      projection pushdown means a column template needing six
                      scenario columns reads only those chunks.
  rollups/{dim}.parquet   pre-aggregated root levels. Small by construction --
                      only dimensions below a cardinality threshold qualify.
  manifest.json       what this batch is, what built it, and how big it came out.

Loading a batch from Parquet measures ~14x faster than repricing it, which is
what makes restarts cheap and lets more than one process serve a firm. Numbers
in docs/hosting.md; `spikes/storage_spike.py` reproduces them.
"""

from __future__ import annotations

import json
import shutil
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterator, Protocol

import polars as pl

# Bump when the artifact layout changes in a way older readers cannot handle.
ARTIFACT_VERSION = 1

POSITIONS = "positions.parquet"
FULL = "batch.parquet"
MANIFEST = "manifest.json"
ROLLUP_DIR = "rollups"

COMPRESSION = "zstd"


# --------------------------------------------------------------------------
# Layout
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class BatchLayout:
    """Where one firm's batch lives.

    Every key is namespaced by firm. Combined with a query container that only
    ever holds one firm's id, a cross-firm read requires both the routing and
    the container's own guard to fail.
    """

    firm_id: str
    batch_id: str

    @property
    def root(self) -> str:
        return f"firms/{self.firm_id}/batches/{self.batch_id}"

    @property
    def positions(self) -> str:
        return f"{self.root}/{POSITIONS}"

    @property
    def full(self) -> str:
        return f"{self.root}/{FULL}"

    @property
    def manifest(self) -> str:
        return f"{self.root}/{MANIFEST}"

    def rollup(self, dimension: str) -> str:
        return f"{self.root}/{ROLLUP_DIR}/{dimension}.parquet"


@dataclass
class Manifest:
    """What a batch is, and what it cost to make."""

    version: int
    firm_id: str
    batch_id: str
    label: str
    timestamp: str
    positions: int
    scenario_labels: list[str]
    shock_config: str
    rollups: dict[str, int] = field(default_factory=dict)  # dimension -> group count
    sizes: dict[str, int] = field(default_factory=dict)    # artifact -> bytes
    timings: dict[str, float] = field(default_factory=dict)  # stage -> seconds

    def to_json(self) -> bytes:
        return json.dumps(asdict(self), indent=2).encode()

    @classmethod
    def from_json(cls, raw: bytes) -> Manifest:
        data = json.loads(raw)
        version = data.get("version")
        if version != ARTIFACT_VERSION:
            raise ValueError(
                f"batch artifact version {version} cannot be read by this build "
                f"(expected {ARTIFACT_VERSION}); rebuild the batch"
            )
        return cls(**data)

    @property
    def built_at(self) -> datetime:
        return datetime.fromisoformat(self.timestamp)


# --------------------------------------------------------------------------
# Stores
# --------------------------------------------------------------------------


class ObjectStore(Protocol):
    """Somewhere to put batch artifacts.

    `uri` exists because Polars reads Parquet by path, not by bytes -- lazy
    scanning is the whole point, so the store has to expose a readable location
    rather than only a download.
    """

    def uri(self, key: str) -> str: ...
    def put(self, key: str, data: bytes) -> None: ...
    def get(self, key: str) -> bytes: ...
    def exists(self, key: str) -> bool: ...
    def list(self, prefix: str) -> list[str]: ...
    def delete_prefix(self, prefix: str) -> None: ...
    def size(self, key: str) -> int: ...
    def scan_options(self) -> dict: ...


class LocalStore:
    """Filesystem-backed. What tests, dev and single-box deployments use."""

    def __init__(self, root: Path | str) -> None:
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)

    def _path(self, key: str) -> Path:
        path = (self.root / key).resolve()
        # Keys come from firm and batch ids; refuse anything that escapes root.
        if not path.is_relative_to(self.root.resolve()):
            raise ValueError(f"key escapes store root: {key}")
        return path

    def uri(self, key: str) -> str:
        return str(self._path(key))

    def put(self, key: str, data: bytes) -> None:
        path = self._path(key)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)

    def get(self, key: str) -> bytes:
        return self._path(key).read_bytes()

    def exists(self, key: str) -> bool:
        return self._path(key).exists()

    def list(self, prefix: str) -> list[str]:
        base = self._path(prefix)
        if not base.exists():
            return []
        return sorted(
            str(p.relative_to(self.root)) for p in base.rglob("*") if p.is_file()
        )

    def delete_prefix(self, prefix: str) -> None:
        path = self._path(prefix)
        if path.is_dir():
            shutil.rmtree(path)
        elif path.exists():
            path.unlink()

    def size(self, key: str) -> int:
        return self._path(key).stat().st_size

    def scan_options(self) -> dict:
        return {}

    # Parquet is written straight to its final path rather than through put(),
    # so a multi-GB batch never has to exist in memory as bytes.
    def open_for_write(self, key: str) -> Path:
        path = self._path(key)
        path.parent.mkdir(parents=True, exist_ok=True)
        return path


class S3Store:
    """S3-backed, for the deployed multi-tenant service.

    boto3 is imported lazily so local development and the test suite need
    neither the dependency nor credentials.
    """

    def __init__(self, bucket: str, prefix: str = "", **client_kwargs) -> None:
        self.bucket = bucket
        self.prefix = prefix.strip("/")
        self._client_kwargs = client_kwargs
        self._client = None

    @property
    def client(self):
        if self._client is None:
            import boto3

            self._client = boto3.client("s3", **self._client_kwargs)
        return self._client

    def _key(self, key: str) -> str:
        return f"{self.prefix}/{key}" if self.prefix else key

    def uri(self, key: str) -> str:
        return f"s3://{self.bucket}/{self._key(key)}"

    def put(self, key: str, data: bytes) -> None:
        self.client.put_object(Bucket=self.bucket, Key=self._key(key), Body=data)

    def get(self, key: str) -> bytes:
        return self.client.get_object(Bucket=self.bucket, Key=self._key(key))["Body"].read()

    def exists(self, key: str) -> bool:
        from botocore.exceptions import ClientError

        try:
            self.client.head_object(Bucket=self.bucket, Key=self._key(key))
            return True
        except ClientError:
            return False

    def list(self, prefix: str) -> list[str]:
        paginator = self.client.get_paginator("list_objects_v2")
        out: list[str] = []
        base = self._key(prefix)
        for page in paginator.paginate(Bucket=self.bucket, Prefix=base):
            for obj in page.get("Contents", []):
                key = obj["Key"]
                out.append(key[len(self.prefix) + 1:] if self.prefix else key)
        return sorted(out)

    def delete_prefix(self, prefix: str) -> None:
        keys = [{"Key": self._key(k)} for k in self.list(prefix)]
        for i in range(0, len(keys), 1000):
            self.client.delete_objects(Bucket=self.bucket, Delete={"Objects": keys[i:i + 1000]})

    def size(self, key: str) -> int:
        return self.client.head_object(Bucket=self.bucket, Key=self._key(key))["ContentLength"]

    def scan_options(self) -> dict:
        return {}


# --------------------------------------------------------------------------
# Reading and writing
# --------------------------------------------------------------------------


def _write_parquet(store: ObjectStore, key: str, frame: pl.DataFrame) -> int:
    """Write a frame, streaming to disk where the store allows it."""
    if isinstance(store, LocalStore):
        path = store.open_for_write(key)
        frame.write_parquet(path, compression=COMPRESSION)
        return path.stat().st_size

    import io

    buffer = io.BytesIO()
    frame.write_parquet(buffer, compression=COMPRESSION)
    data = buffer.getvalue()
    store.put(key, data)
    return len(data)


def scan(store: ObjectStore, key: str) -> pl.LazyFrame:
    """Lazily scan a Parquet artifact, without materializing it."""
    return pl.scan_parquet(store.uri(key), **store.scan_options())


def read(store: ObjectStore, key: str) -> pl.DataFrame:
    return pl.read_parquet(store.uri(key), **store.scan_options())


def write_manifest(store: ObjectStore, layout: BatchLayout, manifest: Manifest) -> None:
    store.put(layout.manifest, manifest.to_json())


def read_manifest(store: ObjectStore, layout: BatchLayout) -> Manifest:
    return Manifest.from_json(store.get(layout.manifest))


def list_batches(store: ObjectStore, firm_id: str) -> list[str]:
    """Batch ids present for a firm, newest last.

    Reads the key layout rather than a database so storage stays the source of
    truth for what actually exists; the control plane's registry is an index
    over it, not a substitute.
    """
    prefix = f"firms/{firm_id}/batches/"
    ids = set()
    for key in store.list(prefix):
        rest = key[len(prefix):]
        if "/" in rest:
            ids.add(rest.split("/", 1)[0])
    return sorted(ids)


def delete_batch(store: ObjectStore, layout: BatchLayout) -> None:
    store.delete_prefix(layout.root)


def iter_firm_keys(store: ObjectStore, firm_id: str) -> Iterator[str]:
    yield from store.list(f"firms/{firm_id}/")


def now_stamp() -> str:
    return datetime.now(timezone.utc).isoformat()
