"""独立场外基金仓库: 完整快照暂存并校验后切换单一发布指针。

PR-1 仅提供单写者发布原语; 跨进程运行租约属于 PR-2, 此处未实现。
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import uuid
from collections import Counter, defaultdict
from dataclasses import dataclass
from datetime import UTC, date, datetime
from itertools import pairwise
from pathlib import Path
from typing import Any

import polars as pl

from app.funds.contracts import (
    SCHEMA_VERSION,
    DatasetName,
    FundCatalog,
    FundCoverage,
    FundEvent,
    FundNav,
    FundSnapshot,
    FundTerm,
)
from app.services.fs_utils import atomic_write_parquet, atomic_write_text

_DATASETS = ("fund_catalog", "fund_nav", "fund_events", "fund_terms", "fund_coverage")
_GENERATION_RE = re.compile(r"^[0-9a-f]{32}$")
_BASE_SCHEMA = {
    "schema_version": pl.Int64,
    "fund_id": pl.Utf8,
    "source_id": pl.Utf8,
    "source_ref": pl.Utf8,
    "revision": pl.Int64,
    "ingested_at": pl.Datetime("us", time_zone="UTC"),
    "published_at": pl.Datetime("us", time_zone="UTC"),
}
_SCHEMAS: dict[str, dict[str, pl.DataType]] = {
    "fund_catalog": {
        **_BASE_SCHEMA,
        "issuer_id": pl.Utf8,
        "source_namespace": pl.Utf8,
        "provider_code": pl.Utf8,
        "share_class": pl.Utf8,
        "name": pl.Utf8,
        "aliases": pl.List(pl.Utf8),
        "offering_type": pl.Utf8,
        "operation_mode": pl.Utf8,
        "investment_category": pl.Utf8,
        "nav_frequency": pl.Utf8,
        "channel": pl.Utf8,
        "currency": pl.Utf8,
        "status": pl.Utf8,
        "valid_from": pl.Date,
        "valid_to": pl.Date,
    },
    "fund_nav": {
        **_BASE_SCHEMA,
        "nav_date": pl.Date,
        "unit_nav": pl.Decimal(28, 10),
        "accumulated_nav": pl.Decimal(28, 10),
        "currency": pl.Utf8,
        "valuation_status": pl.Utf8,
    },
    "fund_events": {
        **_BASE_SCHEMA,
        "event_id": pl.Utf8,
        "event_type": pl.Utf8,
        "effective_date": pl.Date,
        "cash_per_unit": pl.Decimal(28, 10),
        "split_ratio": pl.Decimal(28, 10),
        "record_date": pl.Date,
        "ex_date": pl.Date,
        "payment_date": pl.Date,
    },
    "fund_terms": {
        **_BASE_SCHEMA,
        "term_id": pl.Utf8,
        "valid_from": pl.Date,
        "valid_to": pl.Date,
        "rule_version": pl.Int64,
        "rules_json": pl.Utf8,
    },
    "fund_coverage": {
        **_BASE_SCHEMA,
        "dataset": pl.Utf8,
        "start": pl.Date,
        "end": pl.Date,
        "status": pl.Utf8,
        "checked_at": pl.Datetime("us", time_zone="UTC"),
        "generation": pl.Utf8,
    },
}
_MODELS = {
    "fund_catalog": FundCatalog,
    "fund_nav": FundNav,
    "fund_events": FundEvent,
    "fund_terms": FundTerm,
    "fund_coverage": FundCoverage,
}


@dataclass(frozen=True)
class SnapshotRef:
    generation: str
    manifest: dict[str, Any]


def _digest(path: Path) -> str:
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def _validate_generation(generation: str) -> None:
    if not _GENERATION_RE.fullmatch(generation):
        raise ValueError("invalid generation")


def _check_unique(rows: list, name: str, key) -> None:
    seen: set[tuple] = set()
    for row in rows:
        row_key = key(row)
        if row_key in seen:
            raise ValueError(f"duplicate {name} key: {row_key}")
        seen.add(row_key)


def _check_intervals(rows: list, name: str) -> None:
    # 同一起点的旧修订保留, 区间重叠只检查每个起点的最新修订。
    latest: dict[tuple[str, date], Any] = {}
    for row in rows:
        key = (row.fund_id, row.valid_from)
        if key not in latest or row.revision > latest[key].revision:
            latest[key] = row
    by_fund: dict[str, list] = defaultdict(list)
    for row in latest.values():
        by_fund[row.fund_id].append(row)
    for fund_id, versions in by_fund.items():
        versions.sort(key=lambda row: row.valid_from)
        for previous, current in pairwise(versions):
            if previous.valid_to is None or previous.valid_to > current.valid_from:
                raise ValueError(f"overlap {name} interval for {fund_id}")


def _validate_snapshot(snapshot: FundSnapshot) -> None:
    if not snapshot.catalog:
        raise ValueError("fund_catalog cannot be empty")
    if any(row.offering_type != "public" for row in snapshot.catalog):
        raise ValueError("private or unclassified fund cannot be written to public snapshot")
    datasets = {
        "fund_catalog": snapshot.catalog,
        "fund_nav": snapshot.nav,
        "fund_events": snapshot.events,
        "fund_terms": snapshot.terms,
        "fund_coverage": snapshot.coverage,
    }
    source_ids = {row.source_id for rows in datasets.values() for row in rows}
    if len(source_ids) != 1:
        raise ValueError("snapshot must use exactly one source_id")

    _check_unique(
        snapshot.catalog, "catalog", lambda row: (row.fund_id, row.valid_from, row.revision)
    )
    _check_unique(snapshot.nav, "nav", lambda row: (row.fund_id, row.nav_date, row.revision))
    _check_unique(snapshot.events, "event", lambda row: (row.fund_id, row.event_id, row.revision))
    _check_unique(snapshot.terms, "term", lambda row: (row.fund_id, row.term_id, row.revision))
    _check_unique(
        snapshot.terms,
        "term interval",
        lambda row: (row.fund_id, row.valid_from, row.revision),
    )
    _check_unique(
        snapshot.coverage,
        "coverage",
        lambda row: (row.fund_id, row.dataset, row.start, row.end, row.revision),
    )
    _check_intervals(snapshot.catalog, "catalog")
    _check_intervals(snapshot.terms, "term")
    known_ids = {row.fund_id for row in snapshot.catalog}
    for name, rows in datasets.items():
        for row in rows:
            if row.fund_id not in known_ids:
                raise ValueError(f"{name} references unknown fund_id: {row.fund_id}")
    catalog_by_id: dict[str, list[FundCatalog]] = defaultdict(list)
    for item in snapshot.catalog:
        catalog_by_id[item.fund_id].append(item)
    for row in snapshot.nav:
        if not any(
            item.valid_from <= row.nav_date
            and (item.valid_to is None or row.nav_date < item.valid_to)
            for item in catalog_by_id[row.fund_id]
        ):
            raise ValueError(f"fund_nav has no catalog interval for fund_id: {row.fund_id}")
    if any(row.generation is not None for row in snapshot.coverage):
        raise ValueError("coverage generation is assigned by the repository")


def _rows_to_frame(rows: list, dataset: str) -> pl.DataFrame:
    schema = _SCHEMAS[dataset]
    if not rows:
        return pl.DataFrame(schema=schema)
    converted = []
    for row in rows:
        values = row.model_dump(mode="python")
        if dataset == "fund_terms":
            values["rules_json"] = json.dumps(
                values.pop("rules"),
                ensure_ascii=False,
                sort_keys=True,
                allow_nan=False,
            )
        converted.append(values)
    return pl.from_dicts(converted, schema=schema, strict=True)


def _current_catalog(rows: list[FundCatalog], as_of: date) -> list[FundCatalog]:
    latest: dict[str, FundCatalog] = {}
    for row in rows:
        if row.valid_from > as_of or (row.valid_to is not None and as_of >= row.valid_to):
            continue
        current = latest.get(row.fund_id)
        if current is None or (row.valid_from, row.revision) > (
            current.valid_from,
            current.revision,
        ):
            latest[row.fund_id] = row
    return list(latest.values())


class FundRepository:
    """只读已发布代次; 发布完整快照, 增量合并由后续 pipeline 负责。"""

    def __init__(self, data_dir: Path) -> None:
        self.root = data_dir / "funds"

    def _snapshot_ref(self, generation: str) -> SnapshotRef:
        _validate_generation(generation)
        path = self.root / "snapshots" / generation / "manifest.json"
        try:
            manifest = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise ValueError("snapshot manifest missing or invalid") from exc
        if manifest.get("schema_version") != SCHEMA_VERSION:
            raise ValueError("unsupported schema_version in snapshot manifest")
        if manifest.get("generation") != generation:
            raise ValueError("snapshot manifest generation mismatch")
        if set(manifest.get("datasets", {})) != set(_DATASETS):
            raise ValueError("snapshot manifest dataset list invalid")
        return SnapshotRef(generation, manifest)

    def current(self) -> SnapshotRef | None:
        pointer = self.root / "current.json"
        if not pointer.exists():
            return None
        try:
            data = json.loads(pointer.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise ValueError("fund current pointer invalid") from exc
        if data.get("schema_version") != SCHEMA_VERSION:
            raise ValueError("unsupported schema_version in fund current pointer")
        return self._snapshot_ref(str(data.get("generation", "")))

    def read(self, dataset: DatasetName, generation: str | None = None) -> list:
        if dataset not in _DATASETS:
            raise ValueError(f"unknown fund dataset: {dataset}")
        ref = self._snapshot_ref(generation) if generation is not None else self.current()
        if ref is None:
            return []
        item = ref.manifest["datasets"][dataset]
        relative = f"public/{dataset}.parquet"
        if item.get("path") != relative:
            raise ValueError("snapshot dataset path invalid")
        path = self.root / "snapshots" / ref.generation / relative
        try:
            actual = _digest(path)
        except OSError as exc:
            raise ValueError("snapshot dataset missing") from exc
        if actual != item.get("sha256"):
            raise ValueError("snapshot dataset checksum mismatch")
        frame = pl.read_parquet(path)
        if frame.height != item.get("rows") or frame.schema != _SCHEMAS[dataset]:
            raise ValueError("snapshot dataset schema or row count mismatch")
        result = []
        model = _MODELS[dataset]
        for values in frame.to_dicts():
            if dataset == "fund_terms":
                values["rules"] = json.loads(values.pop("rules_json"))
            result.append(model.model_validate(values))
        return result

    def publish(self, snapshot: FundSnapshot) -> SnapshotRef:
        """同文件系统内写全新代次, 最后一步才切换 current.json。"""
        _validate_snapshot(snapshot)
        generation = uuid.uuid4().hex
        staging = self.root / "staging" / generation
        final = self.root / "snapshots" / generation
        (staging / "public").mkdir(parents=True)
        final.parent.mkdir(parents=True, exist_ok=True)
        rows_by_dataset = {
            "fund_catalog": snapshot.catalog,
            "fund_nav": snapshot.nav,
            "fund_events": snapshot.events,
            "fund_terms": snapshot.terms,
            "fund_coverage": [
                row.model_copy(update={"generation": generation}) for row in snapshot.coverage
            ],
        }
        now = datetime.now(UTC)
        current_catalog = _current_catalog(snapshot.catalog, now.date())
        manifest: dict[str, Any] = {
            "schema_version": SCHEMA_VERSION,
            "generation": generation,
            "created_at": now.isoformat(),
            "source_id": snapshot.catalog[0].source_id,
            "datasets": {},
            "row_counts": {},
            "profile_as_of_date": now.date().isoformat(),
            "classification_counts": {
                field: dict(Counter(getattr(row, field) for row in current_catalog))
                for field in (
                    "offering_type",
                    "operation_mode",
                    "investment_category",
                    "nav_frequency",
                )
            },
        }
        for dataset, rows in rows_by_dataset.items():
            path = staging / "public" / f"{dataset}.parquet"
            atomic_write_parquet(_rows_to_frame(rows, dataset), path)
            manifest["datasets"][dataset] = {
                "path": f"public/{dataset}.parquet",
                "rows": len(rows),
                "sha256": _digest(path),
            }
            manifest["row_counts"][dataset] = len(rows)
        atomic_write_text(
            staging / "manifest.json", json.dumps(manifest, ensure_ascii=False, sort_keys=True)
        )
        os.replace(staging, final)
        atomic_write_text(
            self.root / "current.json",
            json.dumps({"schema_version": SCHEMA_VERSION, "generation": generation}),
        )
        return SnapshotRef(generation, manifest)
