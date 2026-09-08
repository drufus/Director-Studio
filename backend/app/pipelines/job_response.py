"""Common worker ownership and artifact evidence in pipeline job responses."""

from typing import Any

from pydantic import BaseModel, Field

from ..core.schemas import JobRecord


class WorkerJobResponse(BaseModel):
    worker_id: str | None = None
    worker_url: str | None = None
    worker_selected_at: str | None = None
    worker_selection: dict[str, Any] = Field(default_factory=dict)
    memory_admission: dict[str, Any] | None = None
    memory_usage: dict[str, Any] | None = None
    expected_artifacts: dict[str, Any] = Field(default_factory=dict)

    @staticmethod
    def worker_fields(job: JobRecord) -> dict[str, Any]:
        return job.model_dump(include=set(WorkerJobResponse.model_fields))
