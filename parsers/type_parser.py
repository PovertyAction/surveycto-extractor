"""
Parse SurveyCTO question types into components
"""
from typing import Tuple, Optional


def parse_question_type(type_string: str) -> Tuple[str, Optional[str]]:
    """
    Parse question type into base type and choice list reference

    Examples:
        "select_one yesno" -> ("select_one", "yesno")
        "select_multiple services" -> ("select_multiple", "services")
        "integer" -> ("integer", None)
        "text" -> ("text", None)

    Args:
        type_string: Raw type string from survey form

    Returns:
        Tuple of (base_type, choice_list_name)
    """
    if not type_string or not isinstance(type_string, str):
        return ("unknown", None)

    # Clean and split
    parts = str(type_string).strip().split(None, 1)

    if len(parts) == 1:
        return (parts[0], None)

    base_type = parts[0]
    choice_list = parts[1] if len(parts) > 1 else None

    # Handle special cases
    if base_type in ["select_one", "select_multiple"]:
        # Extract choice list name, handling potential "or_other" suffix
        if choice_list:
            choice_list = choice_list.split()[0]  # Take first word only

    return (base_type, choice_list)


def is_select_type(type_string: str) -> bool:
    """Check if question type is a select type"""
    base_type, _ = parse_question_type(type_string)
    return base_type in ["select_one", "select_multiple"]


def is_numeric_type(type_string: str) -> bool:
    """Check if question type is numeric"""
    base_type, _ = parse_question_type(type_string)
    return base_type in ["integer", "decimal"]


def is_text_type(type_string: str) -> bool:
    """Check if question type is text"""
    base_type, _ = parse_question_type(type_string)
    return base_type in ["text", "string"]
