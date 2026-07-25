"""A module with a class and various methods."""


class Calculator:
    """A simple calculator class."""

    def __init__(self, value: int = 0) -> None:
        """Initialize the calculator."""
        self.value = value

    def add(self, x: int) -> int:
        """Add to the current value."""
        self.value += x
        return self.value

    async def async_reset(self) -> None:
        """Async reset method."""
        self.value = 0

    class History:
        """Nested history tracker."""

        def __init__(self) -> None:
            """Initialize history."""
            self.entries: list = []

        def record(self, entry: str) -> None:
            """Record an entry."""
            self.entries.append(entry)
