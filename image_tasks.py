from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any


TASK_COUNT = 8
DEFAULT_WIDTH = "1000"
DEFAULT_HEIGHT = "1000"
DEFAULT_RESOLUTION = f"{DEFAULT_WIDTH}px x {DEFAULT_HEIGHT}px"


@dataclass
class ImageTask:
    index: int
    title: str = ""
    style: str = ""
    content: str = ""
    text_slogan: str = ""
    reference_image: str = ""

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "ImageTask":
        return cls(
            index=int(data.get("index", 0)),
            title=str(data.get("title", "")),
            style=str(data.get("style", "")),
            content=str(data.get("content", "")),
            text_slogan=str(data.get("text_slogan", "")),
            reference_image=str(data.get("reference_image", "")),
        )

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def create_default_tasks(count: int = TASK_COUNT) -> list[ImageTask]:
    return [ImageTask(index=i) for i in range(1, count + 1)]


def normalize_tasks(raw_tasks: list[Any], count: int = TASK_COUNT) -> list[ImageTask]:
    tasks: list[ImageTask] = []

    for i in range(1, count + 1):
        if i - 1 < len(raw_tasks):
            item = raw_tasks[i - 1]
            task = item if isinstance(item, ImageTask) else ImageTask.from_dict(item)
            task.index = i
            tasks.append(task)
        else:
            tasks.append(ImageTask(index=i))

    return tasks
