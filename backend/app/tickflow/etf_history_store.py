"""ETF raw/enriched batch publication with a small roll-forward journal."""

from __future__ import annotations

import json
import os
import shutil
import uuid
from datetime import date
from pathlib import Path

import polars as pl

from app.enriched_generation import EnrichedPublication
from app.indicators.pipeline import ENRICHED_STORAGE_COLS

PENDING = ".etf_history_pending.json"
TABLES = {"kline_etf_daily", "kline_etf_enriched"}


def fingerprint(path):
    try:
        st = path.stat()
        return st.st_mtime_ns, st.st_size
    except FileNotFoundError:
        return None


def raw_snapshot(repo):
    return {
        p: fingerprint(p)
        for p in (repo.store.data_dir / "kline_etf_daily").glob("date=*/part.parquet")
    }


def read_raw(repo, symbols=None):
    paths = list(raw_snapshot(repo))
    if not paths:
        return pl.DataFrame()
    frame = pl.scan_parquet(paths, hive_partitioning=False)
    if symbols is not None:
        frame = frame.filter(pl.col("symbol").is_in(symbols))
    return frame.collect()


def raw_range(repo):
    paths = list(raw_snapshot(repo))
    if not paths:
        return None, None
    return (
        pl.scan_parquet(paths, hive_partitioning=False)
        .select(
            pl.col("date").min().alias("first"),
            pl.col("date").max().alias("last"),
        )
        .collect()
        .row(0)
    )


def pending_plan(repo):
    path = repo.store.data_dir / PENDING
    return json.loads(path.read_text(encoding="utf-8"))["plan"] if path.exists() else None


def _apply(repo, manifest):
    root = repo.store.data_dir
    token = manifest["token"]
    if len(token) != 32 or any(c not in "0123456789abcdef" for c in token):
        raise ValueError("Invalid ETF staging token")
    stage = root / ".etf_history_staging" / token
    factor_version = manifest.get("factor_version")
    if fingerprint(root / "adj_factor_etf/all.parquet") != (
        tuple(factor_version) if factor_version else None
    ):
        raise ValueError("ETF local factors changed; pending publication must be reviewed")
    if not stage.resolve().is_relative_to(root.resolve()):
        raise ValueError("Invalid ETF staging directory")
    # Validate the whole journal before publishing any file or taking ownership.
    for relative in manifest["files"]:
        path = Path(relative)
        if (
            len(path.parts) != 3
            or path.parts[0] not in TABLES
            or not path.parts[1].startswith("date=")
            or path.parts[2] != "part.parquet"
        ):
            raise ValueError("Invalid ETF staging target")
        date.fromisoformat(path.parts[1][5:])
        if not (root / path).resolve().is_relative_to(root.resolve()) or not (
            stage / path
        ).resolve().is_relative_to(stage.resolve()):
            raise ValueError("Invalid ETF staging target")
        if not (stage / path).is_file():
            raise ValueError("Missing ETF staging file")
    publication = EnrichedPublication(root, "etf", recover=True, etf_history=True)
    try:
        publication.begin()
        for relative in manifest["files"]:
            path = Path(relative)
            if (
                path.is_absolute()
                or ".." in path.parts
                or not path.parts
                or path.parts[0] not in TABLES
            ):
                raise ValueError("Invalid ETF staging target")
            target = root / path
            target.parent.mkdir(parents=True, exist_ok=True)
            tmp = target.with_suffix(".etf-tmp")
            shutil.copyfile(stage / path, tmp)
            with tmp.open("r+b") as stream:
                os.fsync(stream.fileno())
            with repo._write_lock:
                os.replace(tmp, target)
                publication.mark_changed()
        with repo._write_lock:
            if fingerprint(root / "adj_factor_etf/all.parquet") != (
                tuple(factor_version) if factor_version else None
            ):
                raise ValueError("ETF local factors changed during publication")
            repo.invalidate_etf_cache()
            publication.commit()
            (root / PENDING).unlink()
        shutil.rmtree(stage, ignore_errors=True)
        repo.refresh_index_views()
    finally:
        del publication  # Tracebacks must not keep an inactive publication claim alive.


def recover_pending(repo):
    path = repo.store.data_dir / PENDING
    if not path.exists():
        return None
    manifest = json.loads(path.read_text(encoding="utf-8"))
    _apply(repo, manifest)
    return manifest.get("result")


def publish_batch(repo, raw, enriched, plan, result, verify):
    root = repo.store.data_dir
    if (root / PENDING).exists():
        raise RuntimeError("ETF batch requires recovery")
    token = uuid.uuid4().hex
    stage = root / ".etf_history_staging" / token
    files, versions = [], []
    storage = enriched.select([c for c in ENRICHED_STORAGE_COLS if c in enriched.columns])
    try:
        for table, frame in [("kline_etf_daily", raw), ("kline_etf_enriched", storage)]:
            for daily in frame.partition_by("date"):
                relative = Path(table) / f"date={daily['date'][0].isoformat()}" / "part.parquet"
                target = root / relative
                version = fingerprint(target)
                old = pl.read_parquet(target) if version else daily.clear()
                merged = (
                    pl.concat([old, daily], how="diagonal_relaxed")
                    .unique(["symbol", "date"], keep="last")
                    .sort(["symbol", "date"])
                )
                out = stage / relative
                out.parent.mkdir(parents=True, exist_ok=True)
                merged.write_parquet(out)
                with out.open("r+b") as stream:
                    os.fsync(stream.fileno())
                files.append(relative.as_posix())
                versions.append((target, version))
        manifest = {
            "token": token,
            "files": files,
            "plan": plan,
            "result": result,
            "factor_version": fingerprint(root / "adj_factor_etf/all.parquet"),
        }
        stage_manifest = stage / "manifest.json"
        with stage_manifest.open("w", encoding="utf-8") as stream:
            json.dump(manifest, stream, ensure_ascii=False)
            stream.flush()
            os.fsync(stream.fileno())
        with repo._write_lock:
            verify()
            if any(fingerprint(p) != v for p, v in versions):
                raise ValueError("ETF data changed before publication")
            os.replace(stage_manifest, root / PENDING)
        _apply(repo, manifest)
    finally:
        if not (root / PENDING).exists() and stage.exists():
            shutil.rmtree(stage)
