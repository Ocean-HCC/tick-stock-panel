"""场外基金标准数据契约。

此模块只验证和表达数据, 不推断基金交易规则或历史披露时间。
"""

from __future__ import annotations

from datetime import UTC, date, datetime
from decimal import Decimal, InvalidOperation
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

SCHEMA_VERSION = 1
DatasetName = Literal["fund_catalog", "fund_nav", "fund_events", "fund_terms", "fund_coverage"]
CoverageDataset = Literal["fund_catalog", "fund_nav", "fund_events", "fund_terms"]


class FundRecord(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    schema_version: Literal[1] = SCHEMA_VERSION
    fund_id: str
    source_id: str
    source_ref: str
    revision: int = Field(ge=1)
    ingested_at: datetime
    published_at: datetime | None = None

    @field_validator("fund_id", "source_id", "source_ref")
    @classmethod
    def nonempty_id(cls, value: str) -> str:
        if not value or not value.strip() or "/" in value or "\\" in value:
            raise ValueError("标识不能为空或包含路径分隔符")
        return value.strip()

    @field_validator("ingested_at", "published_at")
    @classmethod
    def utc_timestamp(cls, value: datetime | None) -> datetime | None:
        if value is None:
            return None
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("时间戳必须带时区")
        return value.astimezone(UTC)

    @model_validator(mode="after")
    def available_when_ingested(self) -> FundRecord:
        if self.published_at is not None and self.published_at > self.ingested_at:
            raise ValueError("published_at 不得晚于 ingested_at")
        return self


class FundCatalog(FundRecord):
    issuer_id: str = Field(min_length=1)
    source_namespace: str = Field(min_length=1)
    provider_code: str = Field(min_length=1)
    share_class: str = Field(min_length=1)
    name: str = Field(min_length=1)
    aliases: list[str] = Field(default_factory=list)
    offering_type: Literal["public", "private", "unknown"]
    operation_mode: Literal["open", "periodic_open", "closed", "other", "unknown"]
    investment_category: Literal[
        "equity",
        "bond",
        "mixed",
        "money_market",
        "qdii",
        "fof",
        "alternative",
        "other",
        "unknown",
    ]
    nav_frequency: Literal["daily", "weekly", "monthly", "irregular", "unknown"]
    channel: Literal["off_exchange"]
    currency: str = Field(min_length=3, max_length=3)
    status: str = Field(min_length=1)
    valid_from: date
    valid_to: date | None = None

    @model_validator(mode="after")
    def valid_interval(self) -> FundCatalog:
        if self.valid_to is not None and self.valid_to <= self.valid_from:
            raise ValueError("valid_to 必须晚于 valid_from")
        if not self.fund_id.startswith(f"{self.source_namespace}:"):
            raise ValueError("fund_id 必须包含 source_namespace 前缀")
        return self


def _decimal_value(value: Any) -> Decimal | None:
    if value is None:
        return None
    if isinstance(value, (float, bool)):
        raise ValueError("金融数值必须是 Decimal 或十进制字符串, 不能是 float/bool")
    try:
        number = Decimal(value)
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise ValueError("金融数值不是合法十进制数") from exc
    if not number.is_finite():
        raise ValueError("金融数值必须有限")
    digits = number.as_tuple().digits
    exponent = number.as_tuple().exponent
    integer_digits = max(len(digits) + exponent, 0)
    if integer_digits > 18 or -exponent > 10:
        raise ValueError("金融数值必须有限且不超过 28 位、10 位小数")
    return number


def _json_safe_rules(value: Any) -> None:
    if isinstance(value, float):
        raise ValueError("条款规则中的金融数值必须使用十进制字符串")
    if isinstance(value, dict):
        if any(not isinstance(key, str) for key in value):
            raise ValueError("条款规则键必须是字符串")
        for item in value.values():
            _json_safe_rules(item)
    elif isinstance(value, list):
        for item in value:
            _json_safe_rules(item)
    elif value is not None and not isinstance(value, (str, int, bool)):
        raise ValueError("条款规则必须是 JSON 值")


class FundNav(FundRecord):
    nav_date: date
    unit_nav: Decimal
    accumulated_nav: Decimal | None = None
    currency: str = Field(min_length=3, max_length=3)
    valuation_status: Literal["official", "estimated", "suspended"]

    @field_validator("unit_nav", "accumulated_nav", mode="before")
    @classmethod
    def decimal_nav(cls, value: Any) -> Decimal | None:
        return _decimal_value(value)

    @model_validator(mode="after")
    def positive_nav(self) -> FundNav:
        if self.unit_nav <= 0 or (self.accumulated_nav is not None and self.accumulated_nav <= 0):
            raise ValueError("净值必须为正")
        return self


class FundEvent(FundRecord):
    event_id: str = Field(min_length=1)
    event_type: Literal[
        "distribution", "split", "conversion", "liquidation", "suspension", "resumption"
    ]
    effective_date: date
    published_at: datetime
    cash_per_unit: Decimal | None = None
    split_ratio: Decimal | None = None
    record_date: date | None = None
    ex_date: date | None = None
    payment_date: date | None = None

    @field_validator("cash_per_unit", "split_ratio", mode="before")
    @classmethod
    def decimal_event(cls, value: Any) -> Decimal | None:
        return _decimal_value(value)

    @model_validator(mode="after")
    def event_fields(self) -> FundEvent:
        if self.event_type == "distribution" and (
            self.cash_per_unit is None
            or self.cash_per_unit <= 0
            or self.record_date is None
            or self.ex_date is None
            or self.payment_date is None
        ):
            raise ValueError("分红事件缺少金额或日期")
        if self.event_type == "split" and (self.split_ratio is None or self.split_ratio <= 0):
            raise ValueError("折算事件缺少正比例")
        return self


class FundTerm(FundRecord):
    term_id: str = Field(min_length=1)
    valid_from: date
    valid_to: date | None = None
    published_at: datetime
    rule_version: int = Field(ge=1)
    # PR-1 只持久化有出处的条款载荷; PR-5 才定义可执行规则并做资格闸。
    rules: dict[str, Any] = Field(default_factory=dict)

    @field_validator("rules")
    @classmethod
    def serializable_rules(cls, value: dict[str, Any]) -> dict[str, Any]:
        _json_safe_rules(value)
        return value

    @model_validator(mode="after")
    def valid_interval(self) -> FundTerm:
        if self.valid_to is not None and self.valid_to <= self.valid_from:
            raise ValueError("valid_to 必须晚于 valid_from")
        return self


class FundCoverage(FundRecord):
    dataset: CoverageDataset
    start: date
    end: date
    status: Literal["complete", "partial", "unavailable", "unchecked"]
    checked_at: datetime
    generation: str | None = None

    @field_validator("checked_at")
    @classmethod
    def utc_checked_at(cls, value: datetime) -> datetime:
        validated = cls.utc_timestamp(value)
        assert validated is not None
        return validated

    @model_validator(mode="after")
    def valid_range(self) -> FundCoverage:
        if self.end < self.start:
            raise ValueError("覆盖结束日期不得早于起始日期")
        return self


class FundSnapshot(BaseModel):
    """一次完整发布的记录集; 空列表表示无记录, 不自动推断覆盖。"""

    model_config = ConfigDict(frozen=True, extra="forbid")
    schema_version: Literal[1] = SCHEMA_VERSION
    catalog: list[FundCatalog] = Field(default_factory=list)
    nav: list[FundNav] = Field(default_factory=list)
    events: list[FundEvent] = Field(default_factory=list)
    terms: list[FundTerm] = Field(default_factory=list)
    coverage: list[FundCoverage] = Field(default_factory=list)
