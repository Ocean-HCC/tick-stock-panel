"""PR-1: 场外基金契约与独立快照仓库。"""

from __future__ import annotations

import json
from datetime import UTC, date, datetime
from decimal import Decimal

import pytest
from pydantic import ValidationError

from app.funds.contracts import (
    FundCatalog,
    FundCoverage,
    FundEvent,
    FundNav,
    FundSnapshot,
    FundTerm,
)
from app.funds.repository import FundRepository

INGESTED = datetime(2026, 1, 9, 12, tzinfo=UTC)
PUBLISHED = datetime(2026, 1, 8, 20, tzinfo=UTC)
FUND_A = "demo:issuer-1:class-a"
FUND_C = "demo:issuer-1:class-c"
SOURCE = {"source_id": "demo", "source_ref": "sample-001", "revision": 1, "ingested_at": INGESTED}


def catalog(fund_id: str = FUND_A, **updates) -> FundCatalog:
    values = {
        **SOURCE,
        "fund_id": fund_id,
        "issuer_id": "issuer-1",
        "source_namespace": "demo",
        "provider_code": "000001",
        "share_class": fund_id.rsplit(":", 1)[-1],
        "name": "示例开放式基金",
        "aliases": [],
        "offering_type": "public",
        "operation_mode": "open",
        "investment_category": "mixed",
        "nav_frequency": "daily",
        "channel": "off_exchange",
        "currency": "CNY",
        "status": "active",
        "valid_from": date(2025, 1, 1),
        "published_at": PUBLISHED,
    }
    values.update(updates)
    return FundCatalog(**values)


def nav(fund_id: str = FUND_A, **updates) -> FundNav:
    values = {
        **SOURCE,
        "fund_id": fund_id,
        "nav_date": date(2026, 1, 8),
        "unit_nav": Decimal("1.2345"),
        "accumulated_nav": Decimal("1.3456"),
        "currency": "CNY",
        "valuation_status": "official",
        "published_at": PUBLISHED,
    }
    values.update(updates)
    return FundNav(**values)


def coverage(dataset: str = "fund_nav", **updates) -> FundCoverage:
    values = {
        **SOURCE,
        "fund_id": FUND_A,
        "dataset": dataset,
        "start": date(2026, 1, 8),
        "end": date(2026, 1, 8),
        "status": "complete",
        "checked_at": INGESTED,
    }
    values.update(updates)
    return FundCoverage(**values)


def sample_snapshot() -> FundSnapshot:
    return FundSnapshot(
        catalog=[catalog(), catalog(FUND_C)],
        nav=[nav(), nav(FUND_C, unit_nav=Decimal("1.1111"))],
        events=[
            FundEvent(
                **SOURCE,
                fund_id=FUND_A,
                event_id="div-1",
                event_type="distribution",
                effective_date=date(2026, 1, 8),
                published_at=PUBLISHED,
                cash_per_unit=Decimal("0.02"),
                record_date=date(2026, 1, 7),
                ex_date=date(2026, 1, 8),
                payment_date=date(2026, 1, 12),
            )
        ],
        terms=[
            FundTerm(
                **SOURCE,
                fund_id=FUND_A,
                term_id="contract-1",
                valid_from=date(2025, 1, 1),
                published_at=PUBLISHED,
                rule_version=1,
                rules={"cutoff_time": "15:00", "timezone": "Asia/Shanghai"},
            )
        ],
        coverage=[coverage("fund_nav"), coverage("fund_events")],
    )


def test_contract_keeps_classification_axes_and_rejects_wrong_channel() -> None:
    private_closed = catalog(offering_type="private", operation_mode="closed")
    assert private_closed.offering_type == "private"
    assert private_closed.operation_mode == "closed"
    assert catalog(FUND_C).fund_id != catalog().fund_id

    with pytest.raises(ValidationError):
        catalog(channel="exchange")
    with pytest.raises(ValidationError):
        catalog(valid_to=date(2025, 1, 1))
    with pytest.raises(ValidationError):
        nav(unit_nav=Decimal("0"))
    with pytest.raises(ValidationError):
        nav(unit_nav=1.2345)  # 浮点数不能无声转成金融 Decimal
    with pytest.raises(ValidationError):
        nav(unit_nav="not-a-decimal")
    with pytest.raises(ValidationError):
        nav(published_at=datetime(2026, 1, 8, 20))  # 无时区禁止
    with pytest.raises(ValidationError):
        nav(published_at=datetime(2026, 1, 10, 20, tzinfo=UTC))
    with pytest.raises(ValidationError):
        nav(unit_nav=Decimal("1E+100"))
    with pytest.raises(ValidationError):
        nav(unit_nav=Decimal("NaN"))
    with pytest.raises(ValidationError):
        FundNav.model_validate({**nav().model_dump(), "schema_version": 2})
    with pytest.raises(ValidationError):
        FundTerm(
            **SOURCE,
            fund_id=FUND_A,
            term_id="bad-rules",
            valid_from=date(2025, 1, 1),
            published_at=PUBLISHED,
            rule_version=1,
            rules={"fee": 0.01},
        )


def test_empty_repository_does_not_touch_stock_data(tmp_path) -> None:
    repo = FundRepository(tmp_path)
    assert repo.current() is None
    assert not (tmp_path / "funds").exists()


def test_publish_roundtrip_manifest_and_generation_pinning(tmp_path) -> None:
    repo = FundRepository(tmp_path)
    first = repo.publish(sample_snapshot())
    assert repo.current() == first
    assert first.generation
    assert first.manifest["row_counts"] == {
        "fund_catalog": 2,
        "fund_nav": 2,
        "fund_events": 1,
        "fund_terms": 1,
        "fund_coverage": 2,
    }
    assert repo.read("fund_catalog", first.generation)[0].share_class == "class-a"
    assert repo.read("fund_nav", first.generation)[0].unit_nav == Decimal("1.2345")
    assert repo.read("fund_terms", first.generation)[0].rules["cutoff_time"] == "15:00"
    assert repo.read("fund_coverage", first.generation)[0].generation == first.generation
    assert list((tmp_path / "funds").glob("**/*.parquet"))
    assert not list((tmp_path / "funds").glob("**/*.tmp"))
    assert not (tmp_path / "user_data" / "watchlist.parquet").exists()

    second = repo.publish(FundSnapshot(catalog=[catalog()], nav=[nav(unit_nav=Decimal("1.3333"))]))
    assert second.generation != first.generation
    assert repo.read("fund_nav")[0].unit_nav == Decimal("1.3333")
    assert repo.read("fund_nav", first.generation)[0].unit_nav == Decimal("1.2345")


def test_rejects_invalid_reference_overlap_and_duplicate_revision_without_publishing(
    tmp_path,
) -> None:
    repo = FundRepository(tmp_path)
    with pytest.raises(ValueError, match="fund_id"):
        repo.publish(FundSnapshot(catalog=[catalog()], nav=[nav(FUND_C)]))
    assert repo.current() is None

    with pytest.raises(ValueError, match="overlap"):
        repo.publish(
            FundSnapshot(
                catalog=[
                    catalog(),
                    catalog(valid_from=date(2025, 6, 1), source_ref="sample-002"),
                ]
            )
        )
    with pytest.raises(ValueError, match="duplicate"):
        repo.publish(FundSnapshot(catalog=[catalog(), catalog()]))
    first_term = sample_snapshot().terms[0]
    with pytest.raises(ValueError, match="duplicate"):
        repo.publish(
            FundSnapshot(
                catalog=[catalog()],
                terms=[first_term, first_term.model_copy(update={"term_id": "another-contract"})],
            )
        )
    assert repo.current() is None


def test_private_catalog_cannot_be_published_into_public_snapshot(tmp_path) -> None:
    repo = FundRepository(tmp_path)
    with pytest.raises(ValueError, match="private"):
        repo.publish(FundSnapshot(catalog=[catalog(offering_type="private")]))
    with pytest.raises(ValueError, match="private"):
        repo.publish(FundSnapshot(catalog=[catalog(offering_type="unknown")]))
    assert repo.current() is None


def test_failed_pointer_write_preserves_old_generation(tmp_path, monkeypatch) -> None:
    repo = FundRepository(tmp_path)
    first = repo.publish(FundSnapshot(catalog=[catalog()], nav=[nav()]))

    def fail_pointer(*_args, **_kwargs) -> None:
        raise OSError("simulated crash")

    monkeypatch.setattr("app.funds.repository.atomic_write_text", fail_pointer)
    with pytest.raises(OSError, match="simulated crash"):
        repo.publish(FundSnapshot(catalog=[catalog()], nav=[nav(unit_nav=Decimal("2"))]))
    assert repo.current() == first
    assert repo.read("fund_nav")[0].unit_nav == Decimal("1.2345")


def test_interrupted_parquet_write_preserves_old_generation(tmp_path, monkeypatch) -> None:
    repo = FundRepository(tmp_path)
    first = repo.publish(FundSnapshot(catalog=[catalog()], nav=[nav()]))
    from app.funds import repository as fund_repository

    original = fund_repository.atomic_write_parquet

    def interrupt(frame, path) -> None:
        if path.name == "fund_nav.parquet":
            path.with_name(path.name + ".tmp").write_bytes(b"PAR1truncated")
            raise OSError("simulated interrupted parquet")
        original(frame, path)

    monkeypatch.setattr(fund_repository, "atomic_write_parquet", interrupt)
    with pytest.raises(OSError, match="simulated interrupted parquet"):
        repo.publish(FundSnapshot(catalog=[catalog()], nav=[nav(unit_nav=Decimal("2"))]))
    assert repo.current() == first
    assert repo.read("fund_nav")[0].unit_nav == Decimal("1.2345")


def test_tampered_manifest_or_unsupported_version_fails_closed(tmp_path) -> None:
    repo = FundRepository(tmp_path)
    first = repo.publish(FundSnapshot(catalog=[catalog()], nav=[nav()]))
    target = tmp_path / "funds" / "snapshots" / first.generation / "public" / "fund_nav.parquet"
    target.write_bytes(b"tampered")
    with pytest.raises(ValueError, match="checksum"):
        repo.read("fund_nav")

    pointer = tmp_path / "funds" / "current.json"
    pointer.write_text(
        json.dumps({"schema_version": 2, "generation": first.generation}), encoding="utf-8"
    )
    with pytest.raises(ValueError, match="schema_version"):
        repo.current()


def test_revisions_are_retained_and_coverage_is_not_inferred(tmp_path) -> None:
    repo = FundRepository(tmp_path)
    first = nav()
    revised = nav(revision=2, source_ref="correction-1", unit_nav=Decimal("1.2555"))
    snapshot = FundSnapshot(
        catalog=[catalog()],
        nav=[first, revised],
        coverage=[coverage(status="unchecked")],
    )
    ref = repo.publish(snapshot)
    assert [row.revision for row in repo.read("fund_nav", ref.generation)] == [1, 2]
    assert repo.read("fund_coverage", ref.generation)[0].status == "unchecked"


def test_profile_counts_latest_catalog_revision_only(tmp_path) -> None:
    repo = FundRepository(tmp_path)
    ref = repo.publish(
        FundSnapshot(
            catalog=[
                catalog(),
                catalog(
                    revision=2,
                    source_ref="corrected-classification",
                    operation_mode="periodic_open",
                ),
                catalog(FUND_C),
            ]
        )
    )
    assert len(repo.read("fund_catalog", ref.generation)) == 3
    assert ref.manifest["classification_counts"]["operation_mode"] == {
        "periodic_open": 1,
        "open": 1,
    }
