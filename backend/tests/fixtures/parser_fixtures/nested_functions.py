"""A module with nested functions."""


def outer(x: int) -> int:
    """Outer function."""

    def inner(y: int) -> int:
        """Inner function."""
        return x + y

    async def async_inner(z: int) -> int:
        """Async inner function."""
        return x + z

    return inner(1)
