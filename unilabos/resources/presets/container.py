from typing import Any

from pylabrobot.resources import Container


class RegularContainer(Container):
    def __init__(self, *args: Any, **kwargs: Any):
        kwargs.pop("pose", None)
        kwargs.setdefault("size_x", 0)
        kwargs.setdefault("size_y", 0)
        kwargs.setdefault("size_z", 0)
        kwargs.setdefault("category", "container")
        super().__init__(*args, **kwargs)


def get_regular_container(name: str = "container") -> RegularContainer:
    return RegularContainer(name=name)
