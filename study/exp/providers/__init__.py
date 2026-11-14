"""Validated, credential-free provider profiles for prospective study runs."""
from __future__ import annotations

import hashlib
import json
import math
import os
import re
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit

DEFAULT = 'minimax-m3.1-flash'
DIRECTORY = Path(__file__).parent
BASE_INVESTIGATOR = Path(__file__).resolve().parents[2] / 'eidos_arms/investigator.json'


def validate(data: dict) -> None:
    required = {'schema', 'id', 'summary', 'wire', 'keychain_item', 'credential_env',
                'upstream', 'investigator', 'served_models', 'price_per_mtok', 'model_usd_per_run'}
    if not isinstance(data, dict) or not required <= data.keys() or data.keys() - required - {'arm2', 'todo'}:
        raise ValueError('unknown or missing provider keys')
    if data['schema'] != 'eidos-provider/1':
        raise ValueError('expected eidos-provider/1')
    for name in ('id', 'summary', 'keychain_item', 'credential_env', 'upstream'):
        if not isinstance(data[name], str) or not data[name].strip():
            raise ValueError(f'provider {name} must be nonempty')
    if not re.fullmatch(r'[a-z0-9][a-z0-9.-]*', data['id']):
        raise ValueError('invalid provider id')
    if not re.fullmatch(r'[A-Z][A-Z0-9_]*_API_KEY', data['credential_env']):
        raise ValueError('credential_env must end in _API_KEY')
    if data['wire'] not in ('openai-chat', 'anthropic-messages'):
        raise ValueError('unknown provider wire')
    _https(data['upstream'])
    patch = data['investigator']
    if not isinstance(patch, dict) or patch.keys() - {'model', 'settings'}:
        raise ValueError('unknown investigator patch keys')
    if 'model' in patch and (not isinstance(patch['model'], str) or not patch['model']):
        raise ValueError('invalid investigator model')
    settings = patch.get('settings', {})
    if not isinstance(settings, dict) or settings.keys() - {'output_mode', 'api_key_env', 'tool_choice'}:
        raise ValueError('unknown provider settings keys')
    if 'output_mode' in settings and settings['output_mode'] not in ('json', 'tool', 'repair'):
        raise ValueError('unknown output_mode')
    if 'api_key_env' in settings and settings['api_key_env'] != data['credential_env']:
        raise ValueError('api_key_env differs from credential_env')
    if 'tool_choice' in settings and settings['tool_choice'] != 'auto':
        raise ValueError('only tool_choice auto is supported')
    patterns = data['served_models']
    if not isinstance(patterns, list) or not patterns or any(not isinstance(p, str) or not p for p in patterns):
        raise ValueError('served_models must contain patterns')
    price = data['price_per_mtok']
    if price is not None and (not isinstance(price, dict) or set(price) != {'input', 'output'}
                              or any(not _number(v) for v in price.values())):
        raise ValueError('invalid price_per_mtok')
    limit = data['model_usd_per_run']
    if limit is not None and (not _number(limit) or limit <= 0 or price is None):
        raise ValueError('USD limit requires a positive limit and price_per_mtok')
    if 'arm2' in data:
        arm2 = data['arm2']
        if not isinstance(arm2, dict) or set(arm2) != {'anthropic_base_url', 'model'}:
            raise ValueError('unknown arm2 keys')
        _https(arm2['anthropic_base_url'])
        if not isinstance(arm2['model'], str) or not arm2['model']:
            raise ValueError('invalid arm2 model')
    if 'todo' in data and not isinstance(data['todo'], str):
        raise ValueError('todo must be a string')


def _number(value):
    return type(value) in (int, float) and math.isfinite(value) and value >= 0


def _https(value):
    if not isinstance(value, str):
        raise ValueError('upstream must be HTTPS without URL credentials')
    url = urlsplit(value)
    if (url.scheme != 'https' or not url.hostname or url.username is not None or url.password is not None
            or url.query or url.fragment):
        raise ValueError('upstream must be HTTPS without URL credentials, query or fragment')


@dataclass(frozen=True)
class Provider:
    data: dict
    sha256: str

    def __getattr__(self, name):
        try:
            return self.data[name]
        except KeyError as exc:
            raise AttributeError(name) from exc

    @property
    def model(self):
        spec = self.investigator.get('model') or json.loads(BASE_INVESTIGATOR.read_text())['model']
        return spec.split(':', 1)[-1]

    def record(self):
        return dict(id=self.id, wire=self.wire, sha256=self.sha256)

    def cli_settings(self):
        if 'arm2' not in self.data:
            raise ValueError(f'provider {self.id} has no arm2 Claude CLI path')
        return self.data['arm2']


def load(provider: str | Provider | None = None, *, allow_placeholder: bool = False) -> Provider:
    if isinstance(provider, Provider):
        # Do not let a previously inspected placeholder bypass live resolution.
        selected = provider
    else:
        name = provider if provider is not None else os.environ.get('EIDOS_STUDY_PROVIDER', DEFAULT)
        if not isinstance(name, str) or not re.fullmatch(r'[a-z0-9][a-z0-9.-]*', name):
            raise ValueError(f'unknown provider: {name!r}')
        path = DIRECTORY / f'{name}.json'
        if not path.is_file():
            raise ValueError(f'unknown provider: {name}')
        raw = path.read_bytes()
        data = json.loads(raw)
        validate(data)
        if data['id'] != name:
            raise ValueError('provider filename/id mismatch')
        selected = Provider(data, hashlib.sha256(raw).hexdigest())
    if not allow_placeholder and 'REPLACE_ME' in selected.investigator.get('model', ''):
        raise ValueError(f'provider {selected.id} contains REPLACE_ME; configure it before running')
    return selected


def check():
    profiles = [load(path.stem, allow_placeholder=True) for path in sorted(DIRECTORY.glob('*.json'))]
    print(f'Validated {len(profiles)} provider profiles (placeholders cannot run)')
