from typing import Any

from pydantic import BaseModel, Field, field_validator, model_validator


class DataSourceConfig(BaseModel):
    host: str = Field(..., min_length=1)
    port: int = Field(default=5432, ge=1, le=65535)
    user: str = Field(..., min_length=1)
    password: str = Field(..., min_length=1)
    database: str = Field(..., min_length=1)
    sslmode: str = Field(default="require", min_length=1)
    allowed_tables: list[str] | None = None

    @field_validator("database")
    @classmethod
    def validate_database(cls, value: str) -> str:
        invalid_tokens = [" ", ";", "--", "/*", "*/", "\\"]
        if any(token in value for token in invalid_tokens):
            raise ValueError("database 包含非法字符")
        return value

    @field_validator("allowed_tables")
    @classmethod
    def validate_allowed_tables(cls, value: list[str] | None) -> list[str] | None:
        if value is None:
            return value
        cleaned = [item.strip() for item in value if item and item.strip()]
        return cleaned or None


class QueryOptions(BaseModel):
    max_rows: int = Field(default=200, ge=1, le=1000)
    retry_on_error: bool = True
    include_explanation: bool = True
    include_chart: bool = False


class DataSourceTestRequest(BaseModel):
    datasource: DataSourceConfig


class QueryRequest(BaseModel):
    question: str = Field(..., min_length=1)
    datasource: DataSourceConfig | None = None
    data_source_id: str | None = None
    options: QueryOptions = Field(default_factory=QueryOptions)

    @model_validator(mode="after")
    def validate_datasource_input(self):
        if self.datasource is None and not self.data_source_id:
            raise ValueError("datasource 与 data_source_id 至少提供一个")
        return self


class QueryExplainRequest(BaseModel):
    question: str = Field(..., min_length=1)
    datasource: DataSourceConfig | None = None
    data_source_id: str | None = None

    @model_validator(mode="after")
    def validate_datasource_input(self):
        if self.datasource is None and not self.data_source_id:
            raise ValueError("datasource 与 data_source_id 至少提供一个")
        return self


class DataSourceCreateRequest(BaseModel):
    name: str = Field(..., min_length=1)
    datasource: DataSourceConfig


class SemanticFieldConfigItem(BaseModel):
    table_name: str = Field(..., min_length=1)
    column_name: str = Field(..., min_length=1)
    field_comment: str = Field(default="")
    field_aliases: list[str] = Field(default_factory=list)
    field_type: str = Field(default="")

    @field_validator("field_aliases")
    @classmethod
    def validate_aliases(cls, value: list[str]) -> list[str]:
        cleaned = [item.strip() for item in value if item and item.strip()]
        return cleaned


class SemanticTableConfigItem(BaseModel):
    table_name: str = Field(..., min_length=1)
    table_comment: str = Field(default="")


class SemanticConfigSaveRequest(BaseModel):
    tables: list[SemanticTableConfigItem] = Field(default_factory=list)
    fields: list[SemanticFieldConfigItem] = Field(default_factory=list)


class ResponseEnvelope(BaseModel):
    code: int
    message: str
    data: dict[str, Any] | None = None
    trace_id: str
    error_type: str | None = None
