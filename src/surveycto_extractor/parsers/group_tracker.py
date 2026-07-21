"""Track group nesting and relevance inheritance in SurveyCTO surveys."""

from dataclasses import dataclass


def _to_str(value) -> str:
    """Coerce None / NaN / non-string scalars to a clean str.

    pandas reads blank XLSForm cells as ``float('nan')``; callers that assume
    string semantics need a single coercion point.
    """
    if value is None:
        return ""
    if isinstance(value, float) and value != value:  # NaN check
        return ""
    return str(value)


def _to_optional_str(value) -> str | None:
    """Coerce None / NaN / empty string to None; otherwise return str(value).

    For ``Optional[str]`` fields where a NaN slipping through would be
    incorrectly truthy under ``if value:`` checks (``bool(float('nan'))`` is
    ``True``).
    """
    if value is None:
        return None
    if isinstance(value, float) and value != value:  # NaN check
        return None
    s = str(value)
    return s if s else None


@dataclass
class Group:
    """Represents a survey group with its metadata."""

    name: str
    label: str
    relevance: str | None
    depth: int

    def __post_init__(self):
        """Honor type annotations regardless of construction path.

        Direct ``Group(name=NaN, ...)`` construction bypasses
        ``GroupStack.push``; normalize here so the dataclass itself enforces
        the contract.
        """
        self.name = _to_str(self.name)
        self.label = _to_str(self.label)
        self.relevance = _to_optional_str(self.relevance)


class GroupStack:
    """Manages group nesting stack for tracking hierarchy."""

    def __init__(self):
        """Initialize an empty group stack and error list."""
        self.stack: list[Group] = []
        self.errors: list[str] = []

    def push(self, name: str, label: str, relevance: str | None) -> None:
        """Add a group to the stack.

        Coerces ``name`` / ``label`` to ``str`` and ``relevance`` to
        ``Optional[str]`` so the ``Group`` dataclass annotations hold even
        when callers bypass the parser layer. Without this, blank ``name``
        cells in XLSForms (which pandas reads as ``float('nan')``) flow into
        the stack and crash downstream consumers that call
        ``'/'.join(group_path)``; a NaN ``relevance`` would slip past
        ``if g.relevance`` filters as truthy (``bool(float('nan')) is True``).
        """
        name = _to_str(name)
        label = _to_str(label)
        relevance = _to_optional_str(relevance)
        depth = len(self.stack)
        group = Group(name=name, label=label, relevance=relevance, depth=depth)
        self.stack.append(group)

    def pop(self, expected_name: str) -> Group | None:
        """Remove a group from the stack and validate name matches."""
        if not self.stack:
            self.errors.append(
                f"Attempted to close group '{expected_name}' but stack is empty"
            )
            return None

        group = self.stack.pop()
        if group.name != expected_name:
            self.errors.append(
                f"Group mismatch: expected to close '{group.name}' but found 'end group' for '{expected_name}'"
            )
        return group

    def get_current_path(self) -> list[str]:
        """Return full hierarchy path as list of group names."""
        return [g.name for g in self.stack]

    def get_inherited_relevance(self) -> list[str]:
        """Return all parent group relevance conditions."""
        return [g.relevance for g in self.stack if g.relevance]

    def is_disabled(self) -> bool:
        """Check if any ancestor group has relevance=0 (disabled)."""
        return any(g.relevance == "0" for g in self.stack)

    def get_depth(self) -> int:
        """Return current nesting depth."""
        return len(self.stack)

    def get_current_label_path(self) -> list[str]:
        """Return full hierarchy path with labels."""
        return [g.label for g in self.stack]

    def is_empty(self) -> bool:
        """Check if stack is empty."""
        return len(self.stack) == 0

    def validate_closed(self) -> bool:
        """Validate that all groups are properly closed."""
        if self.stack:
            unclosed = [g.name for g in self.stack]
            self.errors.append(
                f"Unclosed groups at end of survey: {', '.join(unclosed)}"
            )
            return False
        return True

    def get_errors(self) -> list[str]:
        """Return all accumulated errors."""
        return self.errors
