"""Company reads from this Mac's index or the signed-in Powerset API."""

from __future__ import annotations

import json
import urllib.request
from collections import defaultdict
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, Field

from packs.powerset.primitives.pull_runtime_keys import pull_runtime_keys
from packs.search.primitives.local import local_resolve_companies as resolver
from packs.shared.web.search_scope import LOCAL_INDEX

SUGGEST_LIMIT = 10
SEARCH_MATCH_LIMIT = 500
_SENIORITY_ORDER = {
    'founder': 1, 'c_suite': 2, 'c-suite': 2, 'vp': 3, 'vice-president': 3,
    'partner': 3, 'owner': 3, 'director': 4, 'senior': 5, 'manager': 6,
}
_POSITION_ATTRIBUTES = [
    'person_id', 'company_id', 'position_title', 'seniority_band', 'is_current',
    'start_date_epoch', 'end_date_epoch', 'tenure_years', 'description',
]
_PROFILE_ATTRIBUTES = ['person_id', 'full_name', 'headline', 'linkedin_url', 'public_identifier']


class _SearchRequest(BaseModel):
    scope: str = Field(min_length=1)
    id: str | None = None
    name: str | None = None
    include_people: bool = False
    position_type: Literal['current', 'all'] = 'current'
    people_limit: int = Field(default=10, ge=1, le=2000)
    people_offset: int = Field(default=0, ge=0)
    people_sort: Literal['current', 'status', 'name', 'tenure'] = 'current'
    people_dir: Literal['asc', 'desc'] = 'desc'
    limit: int = Field(default=20, ge=1, le=100)
    offset: int = Field(default=0, ge=0)


@contextmanager
def _local_index():
    path = Path.cwd() / LOCAL_INDEX
    if not path.is_file():
        raise FileNotFoundError('No local index on this Mac')
    store = resolver.LocalDuckDBSearchStore(str(path), read_only=True)
    try:
        yield store
    finally:
        store.conn.close()


def _proxy(path: str, body: dict) -> dict:
    env_file = Path.cwd() / '.env'
    request = urllib.request.Request(
        pull_runtime_keys.api_base(env_file) + '/v2/companies/' + path,
        data=json.dumps(body).encode(),
        headers={'Authorization': f'Bearer {pull_runtime_keys.bearer_token(env_file)}',
                 'Content-Type': 'application/json'},
        method='POST',
    )
    with urllib.request.urlopen(request, timeout=30) as response:
        return json.loads(response.read())


def suggest(q: str, scope: str) -> dict:
    if not scope:
        raise ValueError('Choose a search scope')
    if scope == 'local':
        with _local_index() as store:
            rows = resolver.name_directory_lookup(q, None, top_k=SEARCH_MATCH_LIMIT, store=store) if q.strip() else []
        names = [(row['id'], row['company_name']) for row in rows]
    else:
        rows = _proxy('directory', {'q': q, 'limit': SUGGEST_LIMIT})['companies']
        names = [(row['id'], row['name']) for row in rows]
    suggestions = []
    seen = set()
    for company_id, name in names:
        if name.casefold() in seen:
            continue
        seen.add(name.casefold())
        suggestions.append({'id': company_id, 'display_value': name})
        if len(suggestions) == SUGGEST_LIMIT:
            break
    return {'suggestions': suggestions}


def search(body: dict) -> dict:
    request = _SearchRequest.model_validate(body)
    if request.scope != 'local':
        return _proxy('search', {**{k: v for k, v in body.items() if k != 'scope'}, 'set_id': request.scope})
    with _local_index() as store:
        return _search_local(request, store)


def _date(epoch: int | None) -> str | None:
    return datetime.fromtimestamp(epoch, tz=timezone.utc).strftime('%Y-%m-%d') if epoch else None


def _person(positions: list[dict], profile: dict) -> dict:
    positions.sort(key=lambda row: row.get('start_date_epoch') or 0, reverse=True)
    primary = positions[0]
    total_tenure = sum(row.get('tenure_years') or 0 for row in positions)
    history = []
    for row in positions:
        history.append({
            'position_title': row.get('position_title'),
            'position_description': row.get('description'),
            'seniority_band': row.get('seniority_band'),
            'is_current': row.get('is_current'),
            'start_date': _date(row.get('start_date_epoch')),
            'end_date': _date(row.get('end_date_epoch')),
            'years': round(row['tenure_years'], 1) if row.get('tenure_years') else None,
        })
    return {
        'id': primary['person_id'], 'name': profile.get('full_name'),
        'headline': profile.get('headline'), 'linkedin_url': profile.get('linkedin_url'),
        'public_identifier': profile.get('public_identifier'),
        'position_title': history[0]['position_title'],
        'position_description': history[0]['position_description'],
        'seniority_band': history[0]['seniority_band'],
        'is_current': history[0]['is_current'],
        'start_date': history[0]['start_date'], 'end_date': history[0]['end_date'],
        'tenure_years': round(total_tenure, 1) if total_tenure else None,
        'positions_count': len(positions), 'all_positions': history, 'network': [],
    }


def _people_sort_key(person: dict, request: _SearchRequest) -> tuple:
    name = ' '.join((person['name'] or '').lower().split())
    if request.people_sort == 'name':
        return name, person['id']
    if request.people_sort == 'tenure':
        return person['tenure_years'] or 0, name, person['id']
    return (
        0 if bool(person['is_current']) == (request.people_dir != 'asc') else 1,
        _SENIORITY_ORDER.get(person['seniority_band'], 7),
        -(person['tenure_years'] or 0), name, person['id'],
    )


def _search_local(request: _SearchRequest, store: resolver.LocalDuckDBSearchStore) -> dict[str, Any]:
    if request.id:
        rows = store.filter_only_rows_for_namespace(
            'companies', ('id', 'Eq', request.id), resolver.COMPANY_INCLUDE_ATTRIBUTES,
        )
    elif request.name and request.name.strip():
        rows = resolver.name_directory_lookup(request.name, None, top_k=SEARCH_MATCH_LIMIT, store=store)
    else:
        rows = []
    rows = resolver.dedupe_rows(rows)
    total = len(rows)
    rows = rows[request.offset:request.offset + request.limit]
    company_ids = [row['id'] for row in rows]
    groups = defaultdict(lambda: defaultdict(list))
    profiles = {}
    if company_ids:
        filters = ('company_id', 'In', company_ids)
        if request.position_type == 'current':
            filters = ('And', [filters, ('is_current', 'Eq', True)])
        positions = store.filter_only_rows_for_namespace('people', filters, _POSITION_ATTRIBUTES)
        for row in positions:
            groups[row['company_id']][row['person_id']].append(row)
        if request.include_people and positions:
            person_ids = list({row['person_id'] for row in positions})
            profile_rows = store.filter_only_rows_for_namespace(
                'profiles', ('person_id', 'In', person_ids), _PROFILE_ATTRIBUTES,
            )
            profiles = {row['person_id']: row for row in profile_rows}
    companies = []
    for row in rows:
        people_groups = groups[row['id']]
        company = {
            'id': row['id'], 'name': row.get('company_name'),
            'description': row.get('description'),
            'sector_types': row.get('sector_types') or [], 'entity_types': row.get('entity_types') or [],
            'headcount': int(row['headcount']) if row.get('headcount') else None,
            'funding_total': row.get('funding_total'),
            'city': row.get('city'), 'state': row.get('state'), 'country': row.get('country'),
            'logo_url': row.get('logo_url'), 'linkedin_url': row.get('linkedin_url'),
            'investors': [], 'network': [], 'people_count': len(people_groups),
            'people_offset': request.people_offset, 'people_limit': request.people_limit,
            'people_sort': 'current' if request.people_sort == 'status' else request.people_sort,
            'people_dir': request.people_dir,
            'people_has_more': request.people_offset + request.people_limit < len(people_groups),
        }
        if request.include_people:
            people = [_person(positions, profiles.get(person_id, {})) for person_id, positions in people_groups.items()]
            people.sort(key=lambda person: _people_sort_key(person, request),
                        reverse=request.people_dir == 'desc' and request.people_sort in ('name', 'tenure'))
            company['people'] = people[request.people_offset:request.people_offset + request.people_limit]
        companies.append(company)
    return {'companies': companies, 'total': total, 'limit': request.limit, 'offset': request.offset}
