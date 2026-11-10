"""Company routes over a tiny local index and a mocked cloud API."""

import asyncio
import io
import json
import urllib.error
import urllib.parse
from http import HTTPStatus
from unittest.mock import MagicMock, patch

import duckdb
import pytest

from packs.shared.web.app import AppRoutes
from packs.shared.web import companies


@pytest.fixture
def local_index(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    path = tmp_path / '.powerpacks/search-index/local-search.duckdb'
    path.parent.mkdir(parents=True)
    with duckdb.connect(str(path)) as conn:
        conn.execute('''CREATE TABLE local_companies (
            id VARCHAR, company_name VARCHAR, name_aliases_text VARCHAR,
            website_domain VARCHAR, headcount DOUBLE, city VARCHAR)''')
        conn.execute("""INSERT INTO local_companies VALUES
            ('stripe', 'Stripe', 'Stripe payments', 'stripe.com', 100, 'Example City'),
            ('stripe-copy', 'Stripe', 'Stripe payments', 'stripe.com', 50, NULL),
            ('atlas', 'Atlas Labs', 'Atlas Labs mapping', 'atlas.example', 20, NULL),
            ('domain', 'Example Payments', 'Example Payments', 'paylane.com', 10, NULL)""")
        conn.execute('''CREATE TABLE local_people_positions (
            id VARCHAR, person_id VARCHAR, company_id VARCHAR, position_title VARCHAR,
            is_current BOOLEAN, tenure_years DOUBLE, start_date_epoch BIGINT,
            end_date_epoch BIGINT, seniority_band VARCHAR, description VARCHAR)''')
        conn.execute("""INSERT INTO local_people_positions VALUES
            ('a-old', 'a', 'stripe', 'Engineer', false, 2, 1577836800, 1640995200, 'senior', 'Built things'),
            ('a-new', 'a', 'stripe', 'Director', true, 3, 1640995200, NULL, 'director', 'Leads things'),
            ('b-old', 'b', 'stripe', 'Founder', false, 7, 1262304000, 1483228800, 'founder', NULL),
            ('c-new', 'c', 'atlas', 'Engineer', true, 1, 1640995200, NULL, 'senior', NULL)""")
        conn.execute('''CREATE TABLE local_person_profiles (
            id VARCHAR, person_id VARCHAR, full_name VARCHAR, headline VARCHAR,
            linkedin_url VARCHAR, public_identifier VARCHAR)''')
        conn.execute("""INSERT INTO local_person_profiles VALUES
            ('a', 'a', 'Jordan Bravo', 'Builder', 'https://linkedin.com/in/jordan-bravo', 'jordan-bravo'),
            ('b', 'b', 'Casey Example', 'Founder', 'https://linkedin.com/in/casey-example', 'casey-example'),
            ('c', 'c', 'Sam Example', NULL, NULL, NULL)""")
    return path


def request(path, body=None):
    handler = MagicMock()
    handler.rfile = io.BytesIO(json.dumps(body).encode())
    handler.headers = {'Content-Length': str(len(handler.rfile.getvalue()))}
    route = AppRoutes().get if body is None else AppRoutes().post
    assert route(handler, urllib.parse.urlparse(path))
    raw = handler.wfile.write.call_args.args[0]
    return handler.send_response.call_args.args[0], raw


@pytest.mark.parametrize(('query', 'name'), [
    ('Stripe', 'Stripe'), ('Stri', 'Stripe'), ('stripe.COM', 'Stripe'),
    ('Labs', 'Atlas Labs'), ('mapping', None), ('strpie', 'Stripe'),
    ('PAYLANE', 'Example Payments'), ('paylane.com', 'Example Payments'),
    ('paylnae', 'Example Payments'), ('unrelatedzzzz', None),
])
def test_local_name_search(local_index, query, name):
    status, raw = request('/api/companies/search', {'scope': 'local', 'name': query})
    assert status == HTTPStatus.OK
    rows = json.loads(raw)['companies']
    assert (rows[0]['name'] if rows else None) == name


@pytest.mark.parametrize('name', [None, '', '  '])
def test_no_name_lists_every_company_with_the_true_total(local_index, name):
    with duckdb.connect(str(local_index)) as conn:
        conn.executemany('INSERT INTO local_companies (id, company_name) VALUES (?, ?)',
                         [(f'company-{i}', f'Example {i}') for i in range(15)])
    body = {'scope': 'local', 'limit': 5, 'offset': 5}
    if name is not None:
        body['name'] = name
    result = companies.search(body)
    assert (result['total'], len(result['companies'])) == (19, 5)
    first = companies.search({**body, 'offset': 0})['companies']
    # Most local people first: Stripe (2), then Atlas Labs (1).
    assert [row['id'] for row in first[:2]] == ['stripe', 'atlas']



def test_no_name_orders_by_people_then_headcount(local_index):
    # A blank domain is no match for an empty name; it used to put these rows first.
    with duckdb.connect(str(local_index)) as conn:
        conn.execute("""INSERT INTO local_companies (id, company_name, website_domain, headcount) VALUES
            ('blank', 'Blank Domain', '', 5000), ('big', 'Zeta Defense', 'zeta.example', 90000)""")
        conn.execute("""INSERT INTO local_people_positions (id, person_id, company_id) VALUES
            ('x1', 'x', 'blank'), ('z1', 'z', 'big')""")
    rows = companies.search({'scope': 'local'})['companies']
    assert [row['id'] for row in rows] == ['stripe', 'big', 'blank', 'atlas', 'stripe-copy', 'domain']

@pytest.mark.parametrize(('query', 'minimum', 'expected'), [
    ('Stripe', 75, ['stripe']), ('mapping', 0, ['atlas']),
    ('Stri', 75, ['stripe']), ('strpie', 75, ['stripe']),
])
def test_cli_name_lookup_keeps_tiers_and_filters(local_index, query, minimum, expected):
    backend = companies.resolver.local_backend
    backend.configure_local_backend(local_index)
    try:
        rows = asyncio.run(companies.resolver.company_rows_for_names(
            [query], ('headcount', 'Gt', minimum), top_k=10,
        ))
        assert [row['id'] for row in rows] == expected
    finally:
        backend._local_store_for_path.cache_clear()
        backend.configure_local_backend(None)


@pytest.mark.parametrize('operation', ['search', 'drilldown'])
def test_local_request_releases_database(local_index, operation):
    companies.search({'scope': 'local', 'name': 'Stri',
                      'id': 'stripe' if operation == 'drilldown' else None,
                      'include_people': operation == 'drilldown'})
    with duckdb.connect(str(local_index)) as conn:
        assert conn.execute('SELECT count(*) FROM local_companies').fetchone()[0] == 4


def test_directory_tiers_and_distinct_people_ties(local_index):
    with duckdb.connect(str(local_index)) as conn:
        conn.execute("""INSERT INTO local_companies (id, company_name, website_domain) VALUES
            ('micro', 'Micro', NULL),
            ('microsoft', 'Microsoft', 'microsoft.com'),
            ('micro-labs', 'Micro Labs', NULL),
            ('micro-tools', 'Micro Tools', NULL),
            ('word', 'Example Micro', NULL),
            ('typo', 'Micor', NULL)""")
        conn.execute("""INSERT INTO local_people_positions (id, person_id, company_id) VALUES
            ('m1', 'a', 'microsoft'), ('m2', 'b', 'microsoft'),
            ('l1', 'a', 'micro-labs'), ('l2', 'a', 'micro-labs'), ('l3', 'a', 'micro-labs'),
            ('t1', 'a', 'micro-tools'),
            ('w1', 'a', 'word'), ('w2', 'b', 'word'), ('w3', 'c', 'word')""")
    expected = ['micro', 'microsoft', 'micro-labs', 'micro-tools', 'word', 'typo']
    assert [row['id'] for row in companies.search({'scope': 'local', 'name': 'MICRO'})['companies']] == expected


def test_directory_prefix_without_exact_match(local_index):
    with duckdb.connect(str(local_index)) as conn:
        conn.execute("INSERT INTO local_companies (id, company_name) VALUES ('microsoft', 'Microsoft')")
    assert companies.search({'scope': 'local', 'name': 'Micro'})['companies'][0]['id'] == 'microsoft'


def test_local_list_and_drilldown(local_index):
    status, raw = request('/api/companies/search', {'scope': 'local', 'name': 'Stripe', 'limit': 1})
    result = json.loads(raw)
    assert status == HTTPStatus.OK
    assert (result['total'], result['limit'], result['offset']) == (2, 1, 0)
    row = result['companies'][0]
    assert row['id'] == 'stripe'
    assert row['people_count'] == 1
    assert 'people' not in row
    assert row['network'] == row['investors'] == []
    status, raw = request('/api/companies/search', {
        'scope': 'local', 'id': 'stripe', 'include_people': True,
        'position_type': 'all', 'people_sort': 'name', 'people_dir': 'asc', 'people_limit': 1,
    })
    row = json.loads(raw)['companies'][0]
    assert (row['people_count'], row['people_has_more']) == (2, True)
    assert row['people'][0]['name'] == 'Casey Example'
    result = companies.search({'scope': 'local', 'id': 'stripe', 'include_people': True,
                               'position_type': 'all', 'people_sort': 'name', 'people_dir': 'asc',
                               'people_offset': 1, 'people_limit': 1})
    row = result['companies'][0]
    assert row['people_has_more'] is False
    person = row['people'][0]
    assert person['id'] == 'a'
    assert person['tenure_years'] == 5
    assert person['positions_count'] == 2
    assert person['position_title'] == 'Director'
    assert person['start_date'] == '2022-01-01'
    assert person['end_date'] is None
    assert person['is_current'] is True
    assert person['public_identifier'] == 'jordan-bravo'
    assert person['linkedin_url'] == 'https://linkedin.com/in/jordan-bravo'
    assert person['network'] == []
    assert person['all_positions'][1]['end_date'] == '2022-01-01'
    assert person['all_positions'][0]['position_description'] == 'Leads things'


@pytest.mark.parametrize(('sort', 'direction', 'first'), [
    ('current', 'desc', 'a'), ('status', 'asc', 'b'),
    ('tenure', 'desc', 'b'), ('tenure', 'asc', 'a'), ('name', 'desc', 'a'),
])
def test_local_people_sort(local_index, sort, direction, first):
    result = companies.search({'scope': 'local', 'id': 'stripe', 'include_people': True,
                               'position_type': 'all', 'people_sort': sort, 'people_dir': direction})
    assert result['companies'][0]['people'][0]['id'] == first


def test_no_local_index_is_conflict(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    status, raw = request('/api/companies/search', {'scope': 'local', 'name': 'Stripe'})
    assert status == HTTPStatus.CONFLICT
    assert b'No local index on this Mac' in raw


def test_cloud_proxy():
    body = {'scope': 'set-example', 'set_id': 'ignored', 'name': 'Stripe', 'include_people': True,
            'position_type': 'all', 'people_limit': 2, 'people_offset': 1,
            'people_sort': 'name', 'people_dir': 'asc', 'limit': 3, 'offset': 2}
    cloud = {'companies': [{'id': 'cloud-id', 'people_count': 9}], 'total': 1, 'limit': 3, 'offset': 2}
    response = MagicMock()
    response.__enter__.return_value.read.return_value = json.dumps(cloud).encode()
    with patch('packs.powerset.primitives.pull_runtime_keys.pull_runtime_keys.bearer_token', return_value='test-token'), \
         patch('packs.powerset.primitives.pull_runtime_keys.pull_runtime_keys.api_base', return_value='https://api.example'), \
         patch('urllib.request.urlopen', return_value=response) as upstream:
        status, raw = request('/api/companies/search', body)
        assert status == HTTPStatus.OK
        assert json.loads(raw) == cloud
        sent = upstream.call_args.args[0]
        assert sent.full_url == 'https://api.example/v2/companies/search'
        assert sent.get_header('Authorization') == 'Bearer test-token'
        assert json.loads(sent.data) == {**{k: v for k, v in body.items() if k != 'scope'}, 'set_id': 'set-example'}
        upstream.side_effect = urllib.error.HTTPError(sent.full_url, 503, 'Unavailable', {}, io.BytesIO(b'{"detail":"directory unavailable"}'))
        status, raw = request('/api/companies/search', {'scope': 'set-example', 'limit': 3})
        assert status == HTTPStatus.SERVICE_UNAVAILABLE
        assert json.loads(raw) == {'detail': 'directory unavailable'}


def test_companies_page_shell():
    status, raw = request('/companies')
    assert status == HTTPStatus.OK
    assert b"src='/app/assets/app.js'" in raw
