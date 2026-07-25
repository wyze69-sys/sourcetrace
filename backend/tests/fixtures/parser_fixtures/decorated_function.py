"""A module with a decorated function."""


def my_decorator(func):
    """Simple decorator."""
    return func


@my_decorator
def decorated_greet(name: str) -> str:
    """A decorated greeting function."""
    return f"Hello, {name}!"
