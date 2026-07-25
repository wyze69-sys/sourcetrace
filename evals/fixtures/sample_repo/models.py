"""Domain models for sample repository."""


class RepositoryItem:
    """Representation of an indexed repository item."""

    def __init__(self, item_id: str, name: str) -> None:
        self.item_id = item_id
        self.name = name

    def get_summary(self) -> str:
        """Get summary description of repository item."""
        return f"Item({self.item_id}): {self.name}"
