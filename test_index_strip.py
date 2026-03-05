"""Quick test for index() comparison stripping."""
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent))
from transformers.logic_converter import LogicConverter, clear_strip_log, get_strip_log

# The actual group relevance from hhmem_lastname
# (group stack feeds this as a raw string)
expr = "${hhmem_status} =1 or (index() > ${hh_size_pre} and ${hhmem_addnew} = 1)"
print("Input:", repr(expr))

clear_strip_log()
r = LogicConverter.convert_to_stata(expr, question_types={}, varname="hhmem_lastname")
print("Converted:", repr(r))
print("Strips:", [(e["reason"], e["clause"]) for e in get_strip_log()])
print()
print("Expected: hhmem_status ==1 | (hhmem_addnew == 1)")
