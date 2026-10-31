"""API models for personal correction rules."""

from typing import Literal

from pydantic import BaseModel, Field, field_validator, model_validator


class CorrectionInput(BaseModel):
    source_text: str = Field(min_length=1, max_length=200, strict=True)
    replacement_text: str = Field(min_length=1, max_length=200, strict=True)
    account_biz: list[str]
    enabled: bool = Field(strict=True)

    @field_validator('source_text', 'replacement_text')
    @classmethod
    def nonblank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError('Text must not be blank')
        return value

    @field_validator('account_biz')
    @classmethod
    def normalize_scope(cls, value: list[str]) -> list[str]:
        result = sorted(set(value))
        if len(result) > 100 or any(not biz.strip() for biz in result):
            raise ValueError('Scope must contain at most 100 nonblank accounts')
        return result

    @model_validator(mode='after')
    def different_text(self):
        if self.source_text == self.replacement_text:
            raise ValueError('Source and replacement must differ')
        return self


class CorrectionPreviewInput(CorrectionInput):
    article_id: int = Field(gt=0)
    editing_rule_id: int | None = Field(default=None, gt=0)


class CorrectionPreviewContext(BaseModel):
    before: str
    after: str


class CorrectionPreviewOut(BaseModel):
    status: Literal['ok', 'no_asr', 'not_applicable', 'conflict']
    match_count: int
    contexts: list[CorrectionPreviewContext]
    existing_rule_id: int | None = None


class CorrectionOut(CorrectionInput):
    id: int


class CorrectionListOut(BaseModel):
    items: list[CorrectionOut]
