from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any, ClassVar

from pydantic import BaseModel


class BaseSkill(ABC):
    """Port for agent tools and memory capabilities."""

    name: ClassVar[str] = ""
    description: ClassVar[str] = ""
    args_model: ClassVar[type[BaseModel]] = BaseModel

    def get_json_schema(self) -> dict[str, Any]:
        """Expose a vendor-neutral JSON Schema derived from Pydantic."""

        return self.args_model.model_json_schema()

    @abstractmethod
    async def execute(self, **kwargs: Any) -> str | dict[str, Any]:
        """Execute the tool asynchronously with validated arguments."""
