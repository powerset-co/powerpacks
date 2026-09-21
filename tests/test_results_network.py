"""Saved attribution and one-click pinning in local and portable results."""
import copy
import importlib.util
import json
import tempfile
import threading
import unittest
from pathlib import Path
from http.server import ThreadingHTTPServer

from packs.search.primitives.deep_search.results_web.model import load_searches
from packs.search.primitives.deep_search.results_web.rendering import render_search_body
from packs.search.primitives.deep_search.results_web.server import make_handler
from packs.search.primitives.deep_search.results_web.snapshot import export_snapshot, search_from_snapshot
from tests import test_deep_search_results_web as fixtures
from tests.test_results_snapshot import serve_snapshot


class NetworkResultsTest(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = fixtures.ResultsWebTest()._fixture(self.directory.name, cross_encoder=True)
        self.run = self.root / 'jordan-role'
        self.person = fixtures.ResultsWebTest.PERSON
        path = self.run / 'results.json'
        payload = json.loads(path.read_text())
        payload['person_attribution'] = {self.person: {
            'person_id': self.person, 'total_interactions': 1245,
            'sources': [
                {'channel': 'gmail', 'total_interactions': 1200, 'operator_count': 2},
                {'channel': 'imessage', 'total_interactions': 40, 'operator_count': 1},
                {'channel': 'whatsapp', 'total_interactions': 5, 'operator_count': 1},
                {'channel': 'linkedin', 'total_interactions': 0, 'operator_count': 2}],
            'operators': [
                {'operator_id': 'operator-a', 'operator_name': 'Alex Example',
                 'channels': ['gmail', 'imessage', 'linkedin'], 'gmail_interactions': 1000},
                {'operator_id': 'operator-b', 'operator_name': 'Blair Example',
                 'channels': ['gmail', 'whatsapp', 'linkedin'], 'gmail_interactions': None}],
        }}
        path.write_text(json.dumps(payload))

    def test_sources_are_saved_in_snapshot_and_render_without_lookup(self):
        snapshot = export_snapshot(self.run)
        search = search_from_snapshot(json.loads(json.dumps(snapshot)))
        person = search.candidate(self.person)
        self.assertEqual(person.network_attribution.total_interactions, 1245)
        html = render_search_body(search)
        for text in ('Alex Example', 'Blair Example', '1,200 interactions', '45',
                     'Connected via', 'iMessage', 'WhatsApp', 'data-pin-person='):
            self.assertIn(text, html)
        self.assertEqual(search.candidate(fixtures.ResultsWebTest.UNGRADED).network_attribution, None)

    def test_existing_snapshots_without_attribution_still_load(self):
        snapshot = export_snapshot(self.run)
        for candidate in snapshot['search']['candidates']:
            candidate.pop('network_attribution', None)
        for group in snapshot['search']['groups']:
            for candidate in group['candidates']:
                candidate.pop('network_attribution', None)
        self.assertIsNone(search_from_snapshot(snapshot).candidate(self.person).network_attribution)

    def test_snapshot_rejects_private_identifiers_inside_attribution(self):
        from packs.search.primitives.deep_search.results_web.snapshot import validate_snapshot
        snapshot = copy.deepcopy(export_snapshot(self.run))
        network = snapshot['search']['candidates'][0]['network_attribution']
        network['operators'][0]['gmail_accounts'] = ['private@example.com']
        with self.assertRaisesRegex(ValueError, 'renderer fields'):
            validate_snapshot(snapshot)

    @unittest.skipUnless(importlib.util.find_spec('playwright'), 'Playwright unavailable')
    def test_operator_filter_combines_scores_tags_and_csv_without_writes(self):
        from playwright.sync_api import sync_playwright, expect
        snapshot = json.loads(json.dumps(export_snapshot(self.run)))
        second = fixtures.ResultsWebTest.UNGRADED
        network = next(c['network_attribution'] for c in snapshot['search']['candidates']
                       if c['person_id'] == self.person)
        for candidate in snapshot['search']['candidates']:
            if candidate['person_id'] == second:
                candidate['network_attribution'] = {**network, 'operators': [network['operators'][1]]}
        for pond in snapshot['search']['ponds']:
            for row in pond['candidates']:
                row.update(cross_encoder_score=2 if row['person_id'] == self.person else 1,
                           cross_encoder_score_1_to_5=2 if row['person_id'] == self.person else 1)
        snapshot['tags'] = {'tags': ['Pinned'], 'assignments': {self.person: ['Pinned']}}
        with serve_snapshot(snapshot) as (url, requests), sync_playwright() as p:
            browser = p.chromium.launch(channel='chrome', headless=True)
            page = browser.new_page()
            page.goto(url)
            frame = page.frame_locator('iframe')
            add = frame.get_by_role('button', name='Add operator', exact=True)
            expect(add).to_be_visible(timeout=2000)
            expect(frame.get_by_role('button', name='All results', exact=False)).to_have_count(0)
            rows = frame.locator('.candidate-row:visible')
            expect(rows).to_have_count(3)
            chips = frame.locator('[data-operator-remove]:visible')
            expect(chips).to_have_count(0)
            add.click()
            frame.get_by_role('checkbox', name='Alex Example', exact=True).check()
            expect(rows).to_have_count(1)
            frame.get_by_role('checkbox', name='Blair Example', exact=True).check()
            expect(rows).to_have_count(2)
            expect(chips).to_have_count(2)
            page.keyboard.press('Escape')
            expect(add).to_have_attribute('aria-expanded', 'false')
            frame.get_by_role('button', name='Remove Blair Example', exact=True).click()
            expect(rows).to_have_count(1)
            add.focus()
            page.keyboard.press('Enter')
            frame.get_by_role('checkbox', name='Blair Example', exact=True).check()
            page.keyboard.press('Escape')
            frame.locator('[data-score-filter="2"]').click()
            expect(rows).to_have_count(1)
            frame.locator('[data-result-filter="tagged"]').click()
            expect(rows).to_have_count(1)
            with page.expect_download() as download:
                frame.locator('[data-export-csv]').click()
            csv = Path(download.value.path()).read_text()
            self.assertIn('Jordan Bravo', csv)
            self.assertNotIn('Casey Delta', csv)
            self.assertNotIn('Morgan Echo', csv)
            frame.locator('[data-result-filter="tagged"]').click()
            frame.locator('[data-score-filter="all"]').click()
            expect(rows).to_have_count(2)
            frame.get_by_role('button', name='Remove Alex Example', exact=True).click()
            expect(rows).to_have_count(2)
            frame.get_by_role('button', name='Remove Blair Example', exact=True).click()
            expect(rows).to_have_count(3)
            expect(chips).to_have_count(0)
            self.assertFalse(any('/tags' in request or '/feedback' in request for request in requests))
            for width in (1280, 375):
                page.set_viewport_size({'width': width, 'height': 900})
                expect(add).to_be_visible()
                add.click()
                picker = frame.locator('.operator-picker')
                expect(picker).to_be_visible()
                box = picker.bounding_box()
                self.assertGreaterEqual(box['x'], 0)
                self.assertLessEqual(box['x'] + box['width'], width)
                page.keyboard.press('Escape')
            browser.close()

    @unittest.skipUnless(importlib.util.find_spec('playwright'), 'Playwright unavailable')
    def test_pin_is_the_existing_tag_and_survives_reload_without_reordering(self):
        from playwright.sync_api import sync_playwright, expect
        server = ThreadingHTTPServer(('127.0.0.1', 0), make_handler(
            self.root, lambda: load_searches(self.root), lambda _: {'status': 'submitted'}))
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            with sync_playwright() as p:
                browser = p.chromium.launch(channel='chrome', headless=True)
                page = browser.new_page()
                page.route('https://**', lambda route: route.abort())
                page.goto(f'http://127.0.0.1:{server.server_port}/')
                body = page.locator('[data-search-body="jordan-role"]')
                expect(body).to_have_attribute('data-loaded', 'true')
                before = body.locator('.candidate-row').evaluate_all('(rows) => rows.map(r => r.dataset.personId)')
                pin = body.locator(f'[data-pin-person="{self.person}"]')
                with page.expect_response(lambda r: r.url.endswith('/tags') and r.request.method == 'POST'):
                    pin.click()
                expect(pin).to_have_attribute('aria-pressed', 'true')
                saved = json.loads((self.run / 'tags.json').read_text())
                self.assertEqual(saved['assignments'][self.person], ['Pinned'])
                page.reload()
                expect(body).to_have_attribute('data-loaded', 'true')
                expect(pin).to_have_attribute('aria-pressed', 'true')
                self.assertEqual(before, body.locator('.candidate-row').evaluate_all('(rows) => rows.map(r => r.dataset.personId)'))
                body.locator('[data-result-filter="tagged"]').click()
                expect(body.locator('.candidate-row:visible')).to_have_count(1)
                with page.expect_download() as download:
                    body.locator('[data-export-csv]').click()
                self.assertTrue(download.value.suggested_filename.startswith('pinned_'))
                with page.expect_response(lambda r: r.url.endswith('/tags') and r.request.method == 'POST'):
                    pin.click()
                expect(pin).to_have_attribute('aria-pressed', 'false')
                self.assertNotIn(self.person, json.loads((self.run / 'tags.json').read_text())['assignments'])
                browser.close()
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=5)

    @unittest.skipUnless(importlib.util.find_spec('playwright'), 'Playwright unavailable')
    def test_authenticated_hosted_pin_saves_through_parent_bridge(self):
        from playwright.sync_api import sync_playwright, expect
        snapshot = export_snapshot(self.run)
        with serve_snapshot(snapshot, feedback_enabled=True) as (url, requests), sync_playwright() as p:
            browser = p.chromium.launch(channel='chrome', headless=True)
            page = browser.new_page()
            page.add_init_script('''window.savedTags = [];
                window.addEventListener('message', event => {
                    if (event.data.type !== 'powerpacks:feedback') return;
                    window.savedTags.push(JSON.parse(event.data.values.tagged));
                    event.source.postMessage({type:'powerpacks:feedback-result',
                        requestId:event.data.requestId, status:'submitted'}, '*');
                });''')
            page.goto(url)
            pin = page.frame_locator('iframe').locator(f'[data-pin-person="{self.person}"]')
            expect(pin).to_be_visible()
            expect(pin).to_be_enabled()
            pin.click()
            page.wait_for_function('() => window.savedTags.length === 1')
            snapshot['tags'] = page.evaluate('window.savedTags[0]')
            self.assertEqual(snapshot['tags']['assignments'][self.person], ['Pinned'])
            page.reload()
            expect(pin).to_have_attribute('aria-pressed', 'true')
            pin.click()
            page.wait_for_function('() => window.savedTags.length === 1')
            self.assertEqual(page.evaluate('window.savedTags[0].assignments'), {})
            self.assertFalse(any('/tags' in request for request in requests))
            browser.close()

    @unittest.skipUnless(importlib.util.find_spec('playwright'), 'Playwright unavailable')
    def test_source_operators_align_across_different_identity_widths(self):
        from playwright.sync_api import sync_playwright, expect
        path = self.run / 'results.json'
        payload = json.loads(path.read_text())
        second = fixtures.ResultsWebTest.UNGRADED
        payload['person_attribution'][second] = dict(payload['person_attribution'][self.person], person_id=second)
        path.write_text(json.dumps(payload))
        with serve_snapshot(export_snapshot(self.run)) as (url, _), sync_playwright() as p:
            browser = p.chromium.launch(channel='chrome', headless=True)
            page = browser.new_page()
            page.goto(url)
            operators = page.frame_locator('iframe').locator('[data-network-operators]')
            expect(operators).to_have_count(2)
            for width in (1280, 375):
                page.set_viewport_size({'width': width, 'height': 900})
                right_edges = operators.evaluate_all('(els) => els.map(e => Math.round(e.getBoundingClientRect().right))')
                self.assertEqual(len(set(right_edges)), 1, right_edges)
            browser.close()

    @unittest.skipUnless(importlib.util.find_spec('playwright'), 'Playwright unavailable')
    def test_hosted_network_popover_needs_no_api_and_pin_cannot_mutate(self):
        from playwright.sync_api import sync_playwright, expect
        with serve_snapshot(export_snapshot(self.run)) as (url, requests), sync_playwright() as p:
            browser = p.chromium.launch(channel='chrome', headless=True)
            page = browser.new_page(viewport={'width': 1280, 'height': 900})
            page.route('https://**', lambda route: route.abort())
            errors = []
            page.on('pageerror', lambda e: errors.append(str(e)))
            page.goto(url)
            frame = page.frame_locator('iframe')
            trigger = frame.locator('[data-network-trigger][data-source="gmail"]')
            expect(trigger).to_have_text('~1k')
            expect(frame.locator('[data-network-trigger][data-source="linkedin"]')).to_have_text('')
            popover = frame.locator('.network-popover:popover-open')
            trigger.hover()
            expect(popover).to_be_visible()
            frame.locator('h2').first.hover()
            expect(popover).to_have_count(0)
            trigger.click()
            expect(popover).to_be_visible()
            expect(popover).to_contain_text('Alex Example')
            expect(popover).to_contain_text('1,200 interactions')
            expect(popover).not_to_contain_text('WhatsApp')
            trigger.press('Escape')
            expect(popover).to_have_count(0)
            frame.locator('[data-network-operators]').click()
            expect(popover).to_contain_text('Connected via')
            expect(popover).to_contain_text('Blair Example')
            frame.locator('[data-network-operators]').press('Escape')
            page.set_viewport_size({'width': 375, 'height': 812})
            trigger.focus()
            trigger.press('Enter')
            expect(popover).to_be_visible()
            bounds = popover.bounding_box()
            self.assertLessEqual(bounds['x'] + bounds['width'], 375)
            expect(frame.locator('[data-pin-person]:visible')).to_have_count(0)
            self.assertFalse(any('attribution' in request or 'source-identifiers' in request for request in requests))
            self.assertEqual(errors, [])
            browser.close()
