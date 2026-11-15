"""Download only what the benchmark run needs.

    python -I scripts/benchmark/fetch.py archive ARCHIVE_URL OUT_DIR PATTERN ...   # members of a remote zip
    python -I scripts/benchmark/fetch.py parquet DATASET_ID OUT_DIR PATTERN ...    # files of the Parquet copy

`archive` reads a remote zip with HTTP range requests and extracts only the members matching a pattern, so the
small files the benchmark's runner reads (simple_metrics.csv, inject_time.txt, logts.csv, tracets_*.csv,
cluster_info.json) come without the gigabytes of raw logs and traces around them. A member already on disk at its
full size is skipped, so an interrupted run resumes. `parquet` fetches files of the dataset's Parquet copy (logs
and traces per case) and needs huggingface_hub. docs/real-benchmark.md says where each comes from.
"""
from __future__ import annotations

import fnmatch
import io
import os
import sys
import time
import urllib.request
import zipfile

CHUNK = 1 << 22


class RemoteFile(io.RawIOBase):
    """A file over HTTP that seeks with range requests."""

    def __init__(self, url: str):
        with urllib.request.urlopen(urllib.request.Request(url, method="HEAD"), timeout=60) as r:
            self.url, self.size = r.url, int(r.headers["Content-Length"])
        self.pos = 0

    def seekable(self) -> bool:
        return True

    def readable(self) -> bool:
        return True

    def tell(self) -> int:
        return self.pos

    def seek(self, offset: int, whence: int = 0) -> int:
        self.pos = offset if whence == 0 else self.pos + offset if whence == 1 else self.size + offset
        return self.pos

    def read(self, n: int = -1) -> bytes:
        n = self.size - self.pos if n is None or n < 0 else n
        if n == 0 or self.pos >= self.size:
            return b""
        last = min(self.size, self.pos + n) - 1
        request = urllib.request.Request(self.url, headers={"Range": f"bytes={self.pos}-{last}"})
        for attempt in range(6):
            try:
                with urllib.request.urlopen(request, timeout=120) as r:
                    data = r.read()
                break
            except OSError:
                if attempt == 5:
                    raise
                time.sleep(2 ** attempt)
        self.pos += len(data)
        return data

    def readinto(self, buffer) -> int:
        data = self.read(len(buffer))
        buffer[:len(data)] = data
        return len(data)


def archive(url: str, out: str, patterns: list[str]) -> None:
    z = zipfile.ZipFile(io.BufferedReader(RemoteFile(url), buffer_size=CHUNK))
    for member in z.infolist():
        if member.is_dir() or not any(fnmatch.fnmatch(member.filename, p) for p in patterns):
            continue
        target = os.path.join(out, member.filename)
        if os.path.exists(target) and os.path.getsize(target) == member.file_size:
            continue
        os.makedirs(os.path.dirname(target), exist_ok=True)
        with z.open(member) as src, open(target + ".part", "wb") as dst:
            while chunk := src.read(CHUNK):
                dst.write(chunk)
        os.replace(target + ".part", target)
        print(member.filename, flush=True)


def parquet(dataset_id: str, out: str, patterns: list[str]) -> None:
    from huggingface_hub import snapshot_download
    snapshot_download(repo_id=dataset_id, repo_type="dataset", allow_patterns=patterns, local_dir=out, max_workers=4)


if __name__ == "__main__":
    kind, source, out, *patterns = sys.argv[1:]
    {"archive": archive, "parquet": parquet}[kind](source, out, patterns)
