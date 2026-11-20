"""A tiny JSON-schema subset validator for tool arguments.

Tool calls are validated by the harness so that the model gets precise,
actionable error messages instead of the stage crashing on bad input.
"""
from __future__ import annotations

from typing import Any

_TYPES = {
    'object': lambda v: isinstance(v, dict),
    'array': lambda v: isinstance(v, list),
    'string': lambda v: isinstance(v, str),
    'integer': lambda v: isinstance(v, int) and not isinstance(v, bool),
    'number': lambda v: isinstance(v, (int, float)) and not isinstance(v, bool),
    'boolean': lambda v: isinstance(v, bool),
    'null': lambda v: v is None,
}


def _type_ok(value: Any, expected: Any) -> bool:
    if expected is None:
        return True
    if isinstance(expected, list):
        return any(_TYPES[t](value) for t in expected)
    return _TYPES[expected](value)


def validate(value: Any, schema: dict, path: str = '$') -> list[str]:
    errors: list[str] = []
    expected = schema.get('type')
    if not _type_ok(value, expected):
        return [f'{path}: expected {expected}, got {type(value).__name__}']
    if 'enum' in schema and value not in schema['enum']:
        errors.append(f'{path}: must be one of {schema["enum"]}')
    if isinstance(value, dict) and (expected == 'object' or 'properties' in schema):
        props = schema.get('properties', {})
        for key in schema.get('required', []):
            if key not in value:
                errors.append(f'{path}.{key}: is required')
        if schema.get('additionalProperties') is False:
            for key in value:
                if key not in props:
                    errors.append(f'{path}.{key}: unexpected property')
        for key, sub in props.items():
            if key in value:
                errors.extend(validate(value[key], sub, f'{path}.{key}'))
    if isinstance(value, list):
        if 'minItems' in schema and len(value) < schema['minItems']:
            errors.append(f'{path}: needs at least {schema["minItems"]} item(s)')
        if 'maxItems' in schema and len(value) > schema['maxItems']:
            errors.append(f'{path}: allows at most {schema["maxItems"]} item(s)')
        if 'items' in schema:
            for i, item in enumerate(value):
                errors.extend(validate(item, schema['items'], f'{path}[{i}]'))
    if isinstance(value, str) and 'minLength' in schema and len(value.strip()) < schema['minLength']:
        errors.append(f'{path}: must be at least {schema["minLength"]} characters')
    return errors


def obj(properties: dict, required: list[str] | tuple = (), **extra: Any) -> dict:
    return {'type': 'object', 'properties': properties, 'required': list(required),
            'additionalProperties': False, **extra}


def string(description: str = '', **extra: Any) -> dict:
    out = {'type': 'string', **extra}
    if description:
        out['description'] = description
    return out


def integer(description: str = '') -> dict:
    return {'type': 'integer', **({'description': description} if description else {})}


def boolean(description: str = '') -> dict:
    return {'type': 'boolean', **({'description': description} if description else {})}


def array(items: dict, description: str = '', **extra: Any) -> dict:
    return {'type': 'array', 'items': items, **({'description': description} if description else {}), **extra}
