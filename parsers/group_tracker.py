"""
Track group nesting and relevance inheritance in SurveyCTO surveys
"""
from typing import List, Dict, Optional
from dataclasses import dataclass


@dataclass
class Group:
    """Represents a survey group with its metadata"""
    name: str
    label: str
    relevance: Optional[str]
    depth: int


class GroupStack:
    """Manages group nesting stack for tracking hierarchy"""

    def __init__(self):
        self.stack: List[Group] = []
        self.errors: List[str] = []

    def push(self, name: str, label: str, relevance: Optional[str]) -> None:
        """Add a group to the stack"""
        depth = len(self.stack)
        group = Group(name=name, label=label, relevance=relevance, depth=depth)
        self.stack.append(group)

    def pop(self, expected_name: str) -> Optional[Group]:
        """Remove a group from the stack and validate name matches"""
        if not self.stack:
            self.errors.append(f"Attempted to close group '{expected_name}' but stack is empty")
            return None

        group = self.stack.pop()
        if group.name != expected_name:
            self.errors.append(
                f"Group mismatch: expected to close '{group.name}' but found 'end group' for '{expected_name}'"
            )
        return group

    def get_current_path(self) -> List[str]:
        """Return full hierarchy path as list of group names"""
        return [g.name for g in self.stack]

    def get_inherited_relevance(self) -> List[str]:
        """Return all parent group relevance conditions"""
        return [g.relevance for g in self.stack if g.relevance]

    def is_disabled(self) -> bool:
        """Check if any ancestor group has relevance=0 (disabled)"""
        return any(g.relevance == "0" for g in self.stack)

    def get_depth(self) -> int:
        """Return current nesting depth"""
        return len(self.stack)

    def get_current_label_path(self) -> List[str]:
        """Return full hierarchy path with labels"""
        return [g.label for g in self.stack]

    def is_empty(self) -> bool:
        """Check if stack is empty"""
        return len(self.stack) == 0

    def validate_closed(self) -> bool:
        """Validate that all groups are properly closed"""
        if self.stack:
            unclosed = [g.name for g in self.stack]
            self.errors.append(f"Unclosed groups at end of survey: {', '.join(unclosed)}")
            return False
        return True

    def get_errors(self) -> List[str]:
        """Return all accumulated errors"""
        return self.errors
