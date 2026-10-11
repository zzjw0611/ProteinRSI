# SPDX-License-Identifier: MIT
"""Small JSON edits to a saved draft; validation remains the executor's responsibility."""
from copy import deepcopy
import re
from typing import Any, Literal

from pydantic import Field

from proteinrsi.contracts import Model
from .schema import ContractError, pointer


class DraftEdit(Model):
    op: Literal['add', 'replace', 'remove']
    path: str = Field(min_length=1, max_length=1000)
    value: Any = None


class DraftRepair(Model):
    rationale: str = Field(default='', max_length=2000)
    edits: list[DraftEdit] = Field(min_length=1, max_length=24)


def apply_edits(draft, response):
    """Apply atomically to a copy. No root replacement, expressions or partial writes."""
    edits = DraftRepair.model_validate(response).edits
    result = deepcopy(draft)
    for edit in edits:
        path = edit.path
        if not path.startswith('/') or re.search(r'~(?![01])', path):
            raise ContractError('Edit path must be a JSON pointer to a field', path=path)
        parent_path, raw = path.rsplit('/', 1)
        key = raw.replace('~1', '/').replace('~0', '~')
        parent = pointer(result, parent_path)
        if isinstance(parent, dict):
            if edit.op != 'add' and key not in parent:
                raise ContractError('Edit target does not exist', path=path)
            if edit.op == 'remove':
                del parent[key]
            else:
                parent[key] = deepcopy(edit.value)
        elif isinstance(parent, list):
            if key == '-' and edit.op == 'add':
                index = len(parent)
            elif re.fullmatch(r'0|[1-9][0-9]*', key):
                index = int(key)
            else:
                raise ContractError('Invalid array edit index', path=path)
            if index >= len(parent) + (edit.op == 'add'):
                raise ContractError('Array edit index is out of bounds', path=path)
            if edit.op == 'add':
                parent.insert(index, deepcopy(edit.value))
            elif edit.op == 'remove':
                parent.pop(index)
            else:
                parent[index] = deepcopy(edit.value)
        else:
            raise ContractError('Edit parent is not an object or array', path=path)
    return result


REPAIR_INSTRUCTIONS = '''
Repair the saved failed_draft with only the necessary JSON edits. Return DraftRepair,
not a replacement plan. Paths are JSON pointers; array indexes are zero-based.
Preserve correct fields and earlier repairs. The current validation_errors are active;
repair_history is an audit of earlier errors, not permission to undo those repairs.
The controller applies edits atomically and validates the entire resulting protocol.
'''
