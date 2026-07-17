"""Split master JSON into section-specific files
Phase 3: Section Splitting.
"""

import json
from collections import defaultdict
from pathlib import Path


class SectionSplitter:
    """Split questions by all group levels (nested sections)."""

    def __init__(self, questions: list[dict], output_dir: Path, max_depth: int = None):
        """Initialize splitter.

        Args:
            questions: List of question dictionaries
            output_dir: Output directory for section files
            max_depth: Maximum nesting depth (None = unlimited)

        """
        self.questions = questions
        self.output_dir = Path(output_dir)
        self.max_depth = max_depth
        self.output_dir.mkdir(parents=True, exist_ok=True)

    def split_by_all_levels(self) -> dict[str, tuple]:
        """Split questions by all group path levels (nested sections).

        For a question with group_path = ["a", "b", "c"], create sections:
        - "a" (top level)
        - "a_b" (nested level 1)
        - "a_b_c" (nested level 2)

        If max_depth is set, only create sections up to that depth.

        Returns:
            Dictionary mapping section path to tuple of (questions_list, actual_depth)

        """
        sections = defaultdict(lambda: {"questions": [], "depth": 0})

        for question in self.questions:
            group_path = question.get("group_path", [])

            if not group_path:
                # Questions with no group go to "ungrouped" section
                sections["ungrouped"]["questions"].append(question)
                sections["ungrouped"]["depth"] = 0
            else:
                # Create sections for each level of nesting
                # Limit depth if max_depth is set
                max_level = len(group_path)
                if self.max_depth is not None:
                    max_level = min(max_level, self.max_depth)

                for i in range(1, max_level + 1):
                    # Build section key from path prefix — use / separator
                    # to avoid collisions when group names contain underscores
                    section_key = "/".join(group_path[:i])
                    sections[section_key]["questions"].append(question)
                    sections[section_key]["depth"] = i

        # Convert to dict with just the data we need
        return {k: (v["questions"], v["depth"]) for k, v in sections.items()}

    def save_sections(self, prefix: str = "section") -> dict[str, Path]:
        """Split and save section JSON files for all nesting levels.

        Args:
            prefix: Filename prefix (e.g., "section")

        Returns:
            Dictionary mapping section name to output path

        """
        sections = self.split_by_all_levels()
        output_paths = {}

        for section_name, (section_questions, _) in sections.items():
            # Create safe filename: section keys use / as separator
            safe_name = section_name.replace("/", "_").replace(" ", "_")
            filename = f"{prefix}_{safe_name}.json"
            output_path = self.output_dir / filename

            # Save section
            with open(output_path, "w", encoding="utf-8") as f:
                json.dump(section_questions, f, indent=2, ensure_ascii=False)

            output_paths[section_name] = output_path

        return output_paths

    def split_and_save(self, prefix: str = "section") -> dict[str, Path]:
        """Split questions and save to section files for all nesting levels.

        Args:
            prefix: Filename prefix

        Returns:
            Dictionary mapping section name to output path

        """
        if self.max_depth:
            print(f"\n=== Phase 3: Section Splitting (Max Depth: {self.max_depth}) ===")
        else:
            print("\n=== Phase 3: Section Splitting (All Nesting Levels) ===")

        # Compute sections once, reuse for save + summary
        sections = self.split_by_all_levels()
        output_paths = {}

        for section_name, (section_questions, _) in sections.items():
            safe_name = section_name.replace("/", "_").replace(" ", "_")
            filename = f"{prefix}_{safe_name}.json"
            output_path = self.output_dir / filename
            with open(output_path, "w", encoding="utf-8") as f:
                json.dump(section_questions, f, indent=2, ensure_ascii=False)
            output_paths[section_name] = output_path

        # Print summary grouped by depth
        by_depth = defaultdict(list)
        for section_name, (questions, depth) in sections.items():
            by_depth[depth].append((section_name, questions))

        for depth in sorted(by_depth.keys()):
            if depth == 0:
                print("  Root level:")
            else:
                print(f"  Nesting level {depth}:")

            for section_name, questions in sorted(by_depth[depth]):
                safe_name = section_name.replace("/", "_").replace(" ", "_")
                filename = f"{prefix}_{safe_name}.json"
                indent = "    " if depth > 0 else "  "
                print(f"{indent}[OK] {filename}: {len(questions)} questions")

        print(f"\n  Total: {len(sections)} section files created")
        print()
        return output_paths
