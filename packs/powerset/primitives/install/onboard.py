"""Resume account setup, source imports, and processing in one ordered flow."""
from __future__ import annotations

import argparse
import contextlib
import io
import fcntl
import json
import os
import shlex
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from pathlib import Path

from packs.powerset.primitives.auth import auth
from packs.powerset.primitives.install.status import InstallStatus
from packs.powerset.primitives.install.steps import InstallStep
from packs.powerset.primitives.mcp_install import mcp_install
from packs.powerset.primitives.pull_runtime_keys import pull_runtime_keys as keys

NEEDS_YOU = 10
REQUIRED_KEYS = ("OPENAI_API_KEY", "TURBOPUFFER_API_KEY", "DATABASE_URL")
# Local processing needs only this one; the others serve hosted search.
_PROCESSING_KEY = "OPENAI_API_KEY"


@dataclass(frozen=True)
class Network:
    id: str
    name: str
    person_count: int
    is_personal: bool
    role: str

    @classmethod
    def parse(cls, row: dict) -> "Network":
        return cls(id=str(row["id"]), name=str(row["name"]),
                   person_count=int(row["person_count"]),
                   is_personal=bool(row["is_personal"]), role=str(row["role"]))


class Onboarding:
    def __init__(self, root: Path, *, harnesses: list[str], pid: int, retry_command: str) -> None:
        self.root = root.resolve()
        self.env_path = self.root / ".env"
        self.config = keys._read_env_file(self.root / "packs/powerset/templates/env.powerset.example")
        self.config.update(keys._read_env_file(self.env_path))
        self.config.update(os.environ)
        self.credentials_path = Path(self.config.get("POWERPACKS_CREDENTIALS_PATH", str(auth.DEFAULT_CREDENTIALS_PATH)))
        self.base = self.config.get("POWERSET_API_URL", keys.DEFAULT_API_BASE).rstrip("/")
        self.harnesses = harnesses
        self.pid = pid
        self.retry_command = retry_command
        self.status = InstallStatus(self.root)
        self.step = InstallStep.ACCOUNT
        self.token = ""
        self.email = ""

    def progress(self, event: str, **values) -> dict:
        record = self.status.write(event, step=self.step, pid=self.pid, retry_command=self.retry_command, **values)
        self.step = InstallStep(record["step"])
        return record

    def waiting(self, event: str, **values) -> int:
        print(f"NEEDS YOU: {self.progress(event, **values)['message']}", flush=True)
        return NEEDS_YOU

    def failed(self, event: str, **values) -> int:
        print(f"FAILED: {self.progress(event, **values)['message']}", flush=True)
        return 1

    def log(self, message: str) -> None:
        # CLI errors can echo the supplied bearer; never put it in the log.
        print(message.replace(self.token, "<redacted>") if self.token else message, file=sys.stderr)

    def request(self, path: str, payload: dict | None = None):
        request = urllib.request.Request(self.base + path,
            data=json.dumps(payload).encode() if payload is not None else None,
            headers={"Authorization": f"Bearer {self.token}", "Accept": "application/json",
                     "Content-Type": "application/json"})
        for attempt in range(3):
            try:
                with urllib.request.urlopen(request, timeout=30) as response:
                    return json.load(response)
            except urllib.error.HTTPError as error:
                if error.code not in (429, 502, 503, 504) or attempt == 2:
                    raise
            except (urllib.error.URLError, TimeoutError):
                if attempt == 2:
                    raise
            time.sleep(2 ** attempt)

    def login(self) -> bool:
        self.progress("account.signing_in")
        args = argparse.Namespace(auth0_domain=self.config.get("POWERPACKS_AUTH0_DOMAIN"),
            client_id=self.config.get("POWERPACKS_AUTH0_CLIENT_ID"),
            audience=self.config.get("POWERPACKS_AUTH0_AUDIENCE"),
            scopes=auth.DEFAULT_AUTH0_SCOPES, callback_host=auth.DEFAULT_CALLBACK_HOST,
            callback_port=auth.DEFAULT_CALLBACK_PORT, force_account=False, no_browser=False,
            timeout=auth.DEFAULT_LOGIN_TIMEOUT, credentials_path=self.credentials_path)
        # The existing login opens its own browser and prints the fallback URL to stderr.
        # Its JSON output is unnecessary here and may include remote error details.
        while True:
            output = io.StringIO()
            with contextlib.redirect_stdout(output):
                code = auth.cmd_login(args)
            result = json.loads(output.getvalue()) if output.getvalue().strip() else {}
            if not code or result.get("error") != "login timed out":
                break
            self.progress("account.link_expired")
        if code:
            self.log(f"Account login failed: {result.get('error', 'login did not complete')}")
            return False
        self.token = auth._load_credentials(self.credentials_path)["access_token"]
        return True

    def connect_account(self) -> dict | None:
        self.progress("account.checking")
        reused = True
        try:
            credentials = auth._credentials_with_fresh_token(self.credentials_path,
                self.config.get("POWERPACKS_AUTH0_DOMAIN"), self.config.get("POWERPACKS_AUTH0_CLIENT_ID"))
            self.token = credentials["access_token"]
        except SystemExit:
            reused = False
            if not self.login():
                return None
        try:
            account = self.request("/v2/team/me")
        except urllib.error.HTTPError as exc:
            if exc.code != 401 or not reused:
                raise
            reused = False
            if not self.login():
                return None
            account = self.request("/v2/team/me")
        self.email = account["email"]
        if not self.email:
            raise ValueError("Powerset did not return the signed-in account email")
        # Modal inputs and runs live under this id; servers before 2026-10-05 do not send it.
        if account.get("operator_id"):
            keys.write_env(self.env_path, {"POWERPACKS_OPERATOR_ID": account["operator_id"]})
        self.progress("account.reused" if reused else "account.connected", email=self.email, account_email=self.email)
        return account

    def prepare_credentials(self) -> list[str]:
        self.progress("credentials.preparing")
        values = {}
        responses = {}
        for path, _ in keys.KEY_SOURCES.values():
            if path not in responses:
                responses[path] = keys.fetch_endpoint(self.base, path, self.token)
        for path, (state, payload) in responses.items():
            if state != "error":
                continue
            http_status = payload.get("http_status")
            reason = f"HTTP {http_status}" if http_status else payload.get("reason", "connection failed")
            self.log(f"Search credential request failed at {path}: {reason}")
            if not any(keys.KEY_SOURCES[key][0] == path for key in REQUIRED_KEYS):
                continue
            if http_status:
                raise urllib.error.HTTPError(self.base + path, http_status, "Credential request failed", None, None)
            raise ConnectionError(f"Could not fetch {path}: {reason}")
        for key, (path, field) in keys.KEY_SOURCES.items():
            state, payload = responses[path]
            if state == "ok" and payload and payload.get(field):
                value = str(payload[field]).strip()
                if value:
                    values[key] = value
        keys.write_env(self.env_path, values)
        present = keys._read_env_file(self.env_path)
        missing = [key for key in REQUIRED_KEYS if not present.get(key)]
        if not missing:
            self.progress("credentials.ready")
        return missing

    def connect_tools(self) -> None:
        self.progress("connection.connecting")
        url = self.config["POWERPACKS_MCP_URL"]
        results = []
        for harness in self.harnesses:
            if harness == "codex":
                results.append(mcp_install.codex_install("powerset-search", url, self.token))
            elif harness == "claude-code":
                results.append(mcp_install.claude_install("powerset-search", url, "user", self.token))
        for result in results:
            if not result.get("ok"):
                self.log(f"Agent connection could not be registered: {result.get('error', 'host CLI unavailable')}")
        complete = bool(results) and all(result.get("ok") for result in results)
        self.progress("connection.done" if complete else "connection.skipped")

    def choose_network(self, networks: list[Network], account: dict) -> Network | None:
        selected_id = self.config.get("POWERPACKS_DEFAULT_SET_ID") or self.config.get("POWERSET_DEFAULT_SET_ID")
        if selected_id:
            selected = next((network for network in networks if network.id == selected_id), None)
            if selected:
                return selected
        owned = []
        for network in networks:
            if network.is_personal and network.role == "owner":
                detail = self.request("/v2/sets/" + urllib.parse.quote(network.id, safe=""))
                if any(member["user_id"] == account["user_id"] and member["role"] == "owner"
                       for member in detail["members"]):
                    owned.append(network)
        return owned[0] if len(owned) == 1 else None

    def check_network(self, account: dict) -> int:
        self.progress("network.checking")
        networks = [Network.parse(row) for row in self.request("/v2/sets")]
        selected = self.choose_network(networks, account)
        largest = max(networks, key=lambda network: network.person_count, default=None)
        # The agent can offer the largest network the account has instead.
        alternative = ({"alternative": {"name": largest.name, "person_count": largest.person_count}}
                       if largest and largest.person_count else None)
        if networks and all(network.person_count == 0 for network in networks):
            return self.waiting("network.all_empty", email=self.email)
        if selected is None:
            return self.waiting("network.unconfirmed", email=self.email, details=alternative)
        name = "Personal Network" if selected.is_personal and selected.name == "Personal Connections" else selected.name
        found = {"network": name, "email": self.email, "count": selected.person_count}
        self.progress("network.checking_one", network_name=name, person_count=selected.person_count, **found)
        keys.write_env(self.env_path, {"POWERPACKS_DEFAULT_SET_ID": selected.id})
        if selected.person_count == 0:
            return self.waiting("network.empty", details=alternative, **found)
        escaped_id = urllib.parse.quote(selected.id, safe="")
        contacts = self.request(f"/v2/set-contacts/{escaped_id}?page_size=1")
        count = self.request("/v2/search/count", {"set_id": selected.id, "is_current": True,
                                                  "search_summary": False, "search_company_signal": False})
        if not contacts["leads"] or int(count["count"]) < 1:
            return self.waiting("network.not_searchable", **found)
        record = self.progress("network.ready_small" if selected.person_count < 10 else "network.ready", **found)
        print(f"DONE: {record['message']} Ask me to find someone.", flush=True)
        return 0

    def run(self) -> int:
        try:
            account = self.connect_account()
            if account is None:
                return self.failed("account.login_failed")
            try:
                missing = self.prepare_credentials()
            except urllib.error.HTTPError as exc:
                if exc.code != 401:
                    raise
                if not self.login():
                    return self.failed("account.login_failed")
                account = self.connect_account()
                if account is None:
                    return self.failed("account.login_failed")
                missing = self.prepare_credentials()
            if missing:
                print("Missing search credentials: " + ", ".join(missing), file=sys.stderr)
            if _PROCESSING_KEY in missing:
                return self.waiting("credentials.not_provisioned", email=self.email)
            if missing:
                # Local setup does not use hosted search; say so and keep going.
                self.progress("credentials.hosted_off", email=self.email)
                self.connect_tools()
                return NEEDS_YOU
            self.connect_tools()
            return self.check_network(account)
        except urllib.error.HTTPError as exc:
            print(f"{self.step.value}: HTTP {exc.code} at {urllib.parse.urlsplit(exc.filename).path}", file=sys.stderr)
            if exc.code in (401, 403):
                return self.waiting("account.denied", code=exc.code)
            return self.failed("account.service_failed", code=exc.code)
        except (OSError, ValueError, KeyError, TypeError) as exc:
            reason = str(exc.reason) if isinstance(exc, urllib.error.URLError) else str(exc)
            self.log(f"{self.step.value}: {type(exc).__name__}: {reason}")
        return self.failed("account.error")


def main() -> None:
    from packs.powerset.primitives.install.workflow import SourceOnboarding, _parser
    from packs.powerset.primitives.install.pipeline import ProcessingOnboarding
    from packs.shared.web.server import start_server

    parser = argparse.ArgumentParser(description=__doc__, parents=[_parser(add_help=False)])
    parser.add_argument("--root", type=Path, default=Path.cwd())
    parser.add_argument("--harness", choices=("codex", "claude-code", "pi"), action="append")
    parser.add_argument("--port", type=int)
    parser.add_argument("--approve-spend", choices=("synthesize", "cluster", "enrich", "index"),
                        action="append", default=[])
    args = parser.parse_args()
    root = args.root.resolve()
    status = InstallStatus(root)
    status.directory.mkdir(parents=True, exist_ok=True)
    # The server and agent can both request a resume. Only this process owns work.
    with (status.directory / "onboard.lock").open("a") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            print("NEEDS YOU: Setup is already running.", flush=True)
            raise SystemExit(NEEDS_YOU) from None
        previous = status.read()
        saved, _ = parser.parse_known_args(shlex.split(previous["retry_command"])[1:])
        harnesses = args.harness or saved.harness or ["codex"]
        port = args.port or saved.port or 8765
        # Resolve saved source choices before account progress replaces retry_command.
        flow = SourceOnboarding(root, sources=tuple(args.source),
                                gmail_emails=tuple(args.gmail_email), sync_after=args.sync_after,
                                wacli_store=args.wacli_store, refresh=args.refresh,
                                skip_sources=tuple(args.skip_source))
        flow.retry_command += "".join(f" --harness {harness}" for harness in harnesses)
        flow.retry_command += f" --port {port}"
        try:
            page = start_server(root, port=port, stage="install")
        except (OSError, SystemExit) as error:
            with status.log_path.open("a") as log:
                log.write(str(error) + "\n")
            status.write("install.page_failed", pid=0, retry_command=flow.retry_command)
            print("FAILED: The progress page could not start. Check the installation log.", flush=True)
            raise SystemExit(1) from error
        print(f"STATUS PAGE: {page['url']}", flush=True)
        onboarding = Onboarding(root, harnesses=harnesses, pid=os.getpid(),
                                retry_command=flow.retry_command)
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            code = onboarding.run()
        if code not in (0, NEEDS_YOU) or not onboarding.email:
            print(output.getvalue(), end="", flush=True)
            raise SystemExit(code)
        if code == 0:
            print("Powerset search is ready; local setup will continue.", flush=True)
        payload = flow.run()
        if payload["step"] == InstallStep.DEEP_CONTEXT and (payload.get("action") or {}).get("kind") == "processing":
            payload = ProcessingOnboarding(root, approved_spend=tuple(args.approve_spend)).run()
    prefix = {"completed": "DONE", "waiting": "NEEDS YOU", "running": "NEEDS YOU", "failed": "FAILED"}
    print(f"{prefix[payload['status']]}: {payload['message']}", flush=True)
    raise SystemExit({"completed": 0, "waiting": NEEDS_YOU, "running": NEEDS_YOU, "failed": 1}[payload["status"]])


if __name__ == "__main__":
    main()
