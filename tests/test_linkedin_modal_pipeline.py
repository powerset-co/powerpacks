import io
import os
import unittest
from contextlib import redirect_stderr
from types import SimpleNamespace
from unittest import mock

from packs.indexing.modal import linkedin_modal_pipeline as driver

ZEROS = driver.UNSET_OPERATOR_ID
OTHER = "11111111-1111-4111-8111-111111111111"
ANOTHER = "22222222-2222-4222-8222-222222222222"


class OperatorGuardDecisionTests(unittest.TestCase):
    """Pure decision table: (env value, existing prefixes, allow flag) -> policy."""

    def test_env_set_is_ok_regardless_of_volume_contents(self):
        self.assertEqual(driver.operator_guard_decision(OTHER, [], False), "ok")
        self.assertEqual(driver.operator_guard_decision(OTHER, [ZEROS, ANOTHER], False), "ok")
        self.assertEqual(driver.operator_guard_decision(OTHER, [ANOTHER], True), "ok")

    def test_env_unset_solo_volume_warns(self):
        self.assertEqual(driver.operator_guard_decision(None, [], False), "warn")
        # only the zeros bucket itself exists -> still solo
        self.assertEqual(driver.operator_guard_decision("", [ZEROS], False), "warn")

    def test_env_unset_with_other_operators_refuses(self):
        self.assertEqual(driver.operator_guard_decision(None, [OTHER], False), "refuse")
        self.assertEqual(driver.operator_guard_decision("", [ZEROS, OTHER], False), "refuse")

    def test_whitespace_env_value_counts_as_unset(self):
        self.assertEqual(driver.operator_guard_decision("   ", [OTHER], False), "refuse")

    def test_allow_flag_downgrades_refuse_to_warn(self):
        self.assertEqual(driver.operator_guard_decision(None, [OTHER], True), "warn")
        self.assertEqual(driver.operator_guard_decision(None, [ZEROS, OTHER, ANOTHER], True), "warn")


class ListOperatorPrefixesTests(unittest.TestCase):
    def test_extracts_prefix_names_from_volume_entries(self):
        volume = mock.Mock()
        volume.listdir.return_value = [
            SimpleNamespace(path=f"operators/{ZEROS}"),
            SimpleNamespace(path=f"operators/{OTHER}/"),
        ]
        with mock.patch.object(driver, "get_volume", return_value=volume):
            self.assertEqual(driver.list_operator_prefixes(), [ZEROS, OTHER])
        volume.listdir.assert_called_once_with("operators")

    def test_listing_failure_returns_empty(self):
        with mock.patch.object(driver, "get_volume", side_effect=RuntimeError("no auth")):
            self.assertEqual(driver.list_operator_prefixes(), [])


class RequireOperatorNamespaceTests(unittest.TestCase):
    def _clear_operator_env(self) -> None:
        patcher = mock.patch.dict(os.environ)
        patcher.start()
        self.addCleanup(patcher.stop)
        os.environ.pop("POWERPACKS_OPERATOR_ID", None)

    def test_env_set_passes_without_touching_the_volume(self):
        with mock.patch.dict(os.environ, {"POWERPACKS_OPERATOR_ID": OTHER}), \
                mock.patch.object(driver, "list_operator_prefixes") as listing:
            driver.require_operator_namespace(allow_default=False)
        listing.assert_not_called()

    def test_refuses_on_a_volume_other_operators_use(self):
        self._clear_operator_env()
        with mock.patch.object(driver, "list_operator_prefixes", return_value=[ZEROS, OTHER]):
            with self.assertRaises(SystemExit) as ctx:
                driver.require_operator_namespace(allow_default=False)
        message = str(ctx.exception)
        self.assertIn("POWERPACKS_OPERATOR_ID", message)
        self.assertIn("--allow-default-operator", message)
        self.assertIn(ZEROS, message)

    def test_allow_flag_bypasses_refusal_but_still_warns(self):
        self._clear_operator_env()
        err = io.StringIO()
        with mock.patch.object(driver, "list_operator_prefixes", return_value=[OTHER]), \
                redirect_stderr(err):
            driver.require_operator_namespace(allow_default=True)
        self.assertIn("warning:", err.getvalue())
        self.assertIn(ZEROS, err.getvalue())

    def test_solo_volume_warns_and_continues(self):
        self._clear_operator_env()
        err = io.StringIO()
        with mock.patch.object(driver, "list_operator_prefixes", return_value=[]), \
                redirect_stderr(err):
            driver.require_operator_namespace(allow_default=False)
        self.assertIn("warning:", err.getvalue())

    def test_listing_failure_degrades_to_the_warning_path(self):
        self._clear_operator_env()
        err = io.StringIO()
        with mock.patch.object(driver, "get_volume", side_effect=RuntimeError("network down")), \
                redirect_stderr(err):
            driver.require_operator_namespace(allow_default=False)
        self.assertIn("warning:", err.getvalue())


class GatedFlagWiringTests(unittest.TestCase):
    def test_gated_command_set(self):
        self.assertEqual(
            driver.GATED_COMMANDS,
            {"pipeline", "import-linkedin", "index-people", "upload", "process", "run", "amplify"},
        )

    def test_gated_commands_accept_allow_default_operator(self):
        parser = driver.build_parser()
        args = parser.parse_args(["upload", "--allow-default-operator"])
        self.assertTrue(args.allow_default_operator)
        args = parser.parse_args(["pipeline", "--csv", "x"])
        self.assertFalse(args.allow_default_operator)
        args = parser.parse_args(["process", "--dataset", "real", "--allow-default-operator"])
        self.assertTrue(args.allow_default_operator)

    def test_read_only_download_is_not_gated(self):
        self.assertNotIn("download", driver.GATED_COMMANDS)
        parser = driver.build_parser()
        with redirect_stderr(io.StringIO()):
            with self.assertRaises(SystemExit):
                parser.parse_args(["download", "--label", "x", "--allow-default-operator"])


if __name__ == "__main__":
    unittest.main()
