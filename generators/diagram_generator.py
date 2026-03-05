"""
Generate text tree diagrams of survey structure
Phase 2: Structure Diagram Generation
"""
import pandas as pd
from pathlib import Path
from typing import List, Tuple


class DiagramGenerator:
    """Generate hierarchical text diagrams of survey structure"""

    def __init__(self, survey_df: pd.DataFrame, output_dir: Path):
        """
        Initialize generator

        Args:
            survey_df: Survey sheet DataFrame
            output_dir: Output directory for diagram files
        """
        self.survey_df = survey_df
        self.output_dir = Path(output_dir)
        self.output_dir.mkdir(parents=True, exist_ok=True)

    def _get_tree_chars(self, is_last: bool, depth: int) -> str:
        """Get tree drawing characters for given position"""
        if depth == 0:
            return ""
        if is_last:
            return "    " * (depth - 1) + "└── "
        else:
            return "    " * (depth - 1) + "├── "

    def _format_group_line(self, name: str, label: str, relevance: str, is_repeat: bool, depth: int, is_last: bool) -> str:
        """Format a single group line with tree characters"""
        chars = self._get_tree_chars(is_last, depth)
        repeat_marker = " [REPEAT]" if is_repeat else ""

        line = f"{chars}{name}"
        if label and label != name:
            line += f" ({label})"
        line += repeat_marker

        if relevance:
            line += f"\n{'    ' * depth}Relevance: {relevance}"

        return line

    def generate_structure(self) -> List[str]:
        """
        Generate hierarchical structure as list of lines

        Returns:
            List of formatted text lines
        """
        lines = []
        lines.append("SURVEY STRUCTURE")
        lines.append("=" * 80)
        lines.append("")

        group_stack = []  # Stack of (name, label, relevance, is_repeat, depth)
        last_at_depth = {}  # Track if group is last at its depth

        for idx, row in self.survey_df.iterrows():
            row_type = row.get("type", "")

            if row_type == "begin group":
                name = row.get("name", f"unnamed_group_{idx}")
                label = row.get("label", "")
                relevance = row.get("relevance", "")
                depth = len(group_stack) + 1

                # Determine if this is the last group at this level
                # (Simple heuristic: check if next row at same level is end group)
                is_last = False

                group_stack.append((name, label, relevance, False, depth))
                line = self._format_group_line(name, label, relevance, False, depth, is_last)
                lines.append(line)

            elif row_type == "begin repeat":
                name = row.get("name", f"unnamed_repeat_{idx}")
                label = row.get("label", "")
                relevance = row.get("relevance", "")
                depth = len(group_stack) + 1

                is_last = False
                group_stack.append((name, label, relevance, True, depth))
                line = self._format_group_line(name, label, relevance, True, depth, is_last)
                lines.append(line)

            elif row_type in ["end group", "end repeat"]:
                if group_stack:
                    group_stack.pop()
                    # Add vertical spacing after closing major groups
                    if len(group_stack) == 0:
                        lines.append("")

        return lines

    def save_diagram(self, output_name: str) -> Path:
        """
        Generate and save structure diagram

        Args:
            output_name: Output filename

        Returns:
            Path to created text file
        """
        lines = self.generate_structure()
        output_path = self.output_dir / output_name

        with open(output_path, 'w', encoding='utf-8') as f:
            f.write('\n'.join(lines))

        print(f"[OK] Created {output_name}: {len(lines)} lines")
        return output_path

    def generate_and_save(self, output_name: str) -> Path:
        """
        Convenience method to generate and save diagram

        Args:
            output_name: Output filename

        Returns:
            Path to created file
        """
        return self.save_diagram(output_name)
