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
from packs.powerset.primitives.install.status import InstallState, InstallStatus, InstallStep
from packs.powerset.primitives.mcp_install import mcp_install
from packs.powerset.primitives.pull_runtime_keys import pull_runtime_keys as keys

NEEDS_YOU = 10
REQUIRED_KEYS = ("OPENAI_API_KEY", "TURBOPUFFER_API_KEY", "DATABASE_URL")


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

    def progress(self, step: InstallStep, state: InstallState, message: str, **summary) -> None:
        self.step = step
        self.status.write(step=step, status=state, message=message, pid=self.pid,
                          retry_command=self.retry_command, **summary)

    def waiting(self, message: str) -> int:
        self.progress(self.step, InstallState.WAITING, message)
        print(f"NEEDS YOU: {message}", flush=True)
        return NEEDS_YOU

    def failed(self, message: str) -> int:
        self.progress(self.step, InstallState.FAILED, message)
        print(f"FAILED: {message}", flush=True)
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
        self.progress(InstallStep.ACCOUNT, InstallState.WAITING,
                      "Waiting for account login. Sign in in the browser; setup will continue automatically.")
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
            self.progress(InstallStep.ACCOUNT, InstallState.WAITING,
                          "The sign-in link expired. Opening a fresh one.")
        if code:
            self.log(f"Account login failed: {result.get('error', 'login did not complete')}")
            return False
        self.token = auth._load_credentials(self.credentials_path)["access_token"]
        return True

    def connect_account(self) -> dict | None:
        self.progress(InstallStep.ACCOUNT, InstallState.RUNNING, "Checking your account")
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
        self.progress(InstallStep.ACCOUNT, InstallState.SKIPPED if reused else InstallState.COMPLETED,
                      f"Already signed in as {self.email}" if reused else f"Connected as {self.email}",
                      account_email=self.email)
        return account

    def prepare_credentials(self) -> list[str]:
        self.progress(InstallStep.CREDENTIALS, InstallState.RUNNING, "Preparing search access")
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
        missing = [key for key in REQUIRED_KEYS if key not in values]
        if not missing:
            self.progress(InstallStep.CREDENTIALS, InstallState.COMPLETED, "Search access is ready")
        return missing

    def connect_tools(self) -> None:
        self.progress(InstallStep.CONNECTION, InstallState.RUNNING, "Connecting Powerpacks to your agent")
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
        self.progress(InstallStep.CONNECTION, InstallState.COMPLETED if complete else InstallState.SKIPPED,
                      "Agent connection configured" if complete else
                      "Agent connection was skipped. Checking direct Powerset access next.")

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
        self.progress(InstallStep.NETWORK, InstallState.RUNNING, "Checking your network")
        networks = [Network.parse(row) for row in self.request("/v2/sets")]
        selected = self.choose_network(networks, account)
        alternative = max(networks, key=lambda network: network.person_count, default=None)
        alternative_message = (f" {alternative.name} also has {alternative.person_count:,} people; "
                               "tell me in chat if you want to use it." if alternative and alternative.person_count else "")
        if networks and all(network.person_count == 0 for network in networks):
            return self.waiting(f"All available networks for {self.email} have 0 people. Tell me in chat "
                                "whether to switch accounts or connect your contacts.")
        if selected is None:
            return self.waiting(f"Connected as {self.email}, but your personal or previously selected network "
                                "could not be confirmed. Tell me in chat whether to switch accounts or choose a network."
                                + alternative_message)
        name = "Personal Network" if selected.is_personal and selected.name == "Personal Connections" else selected.name
        self.progress(InstallStep.NETWORK, InstallState.RUNNING, f"Checking {name} for {self.email}",
                      network_name=name, person_count=selected.person_count)
        keys.write_env(self.env_path, {"POWERPACKS_DEFAULT_SET_ID": selected.id})
        if selected.person_count == 0:
            return self.waiting(f"{name} for {self.email} has 0 people. Tell me in chat whether to switch "
                                "accounts or connect your contacts." + alternative_message)
        escaped_id = urllib.parse.quote(selected.id, safe="")
        contacts = self.request(f"/v2/set-contacts/{escaped_id}?page_size=1")
        count = self.request("/v2/search/count", {"set_id": selected.id, "is_current": True,
                                                  "search_summary": False, "search_company_signal": False})
        if not contacts["leads"] or int(count["count"]) < 1:
            return self.waiting(f"{name} for {self.email} has {selected.person_count:,} people, but its searchable "
                                "profiles are not ready. Ask me in chat to check the network before searching.")
        warning = (" This is a small network; you may want another network or account." if selected.person_count < 10 else "")
        message = f"{name} for {self.email} is ready: {selected.person_count:,} people in this network." + warning
        self.progress(InstallStep.NETWORK, InstallState.COMPLETED, message)
        print(f"DONE: {message} Ask me to find someone.", flush=True)
        return 0

    def run(self) -> int:
        try:
            account = self.connect_account()
            if account is None:
                return self.failed("Account login did not finish. Tell me in chat to try again; I will reopen the sign-in page.")
            try:
                missing = self.prepare_credentials()
            except urllib.error.HTTPError as exc:
                if exc.code != 401:
                    raise
                if not self.login():
                    return self.failed("Account login did not finish. Tell me in chat to try signing in again.")
                account = self.connect_account()
                if account is None:
                    return self.failed("Account login could not be verified. Tell me in chat to try again.")
                missing = self.prepare_credentials()
            if missing:
                print("Missing search credentials: " + ", ".join(missing), file=sys.stderr)
                return self.waiting(f"Connected as {self.email}, but search access has not been provisioned. "
                                    "Ask Powerset to finish enabling search for this account, then tell me to retry.")
            self.connect_tools()
            return self.check_network(account)
        except urllib.error.HTTPError as exc:
            print(f"{self.step.value}: HTTP {exc.code} at {urllib.parse.urlsplit(exc.filename).path}", file=sys.stderr)
            message = f"Powerset could not complete this check (HTTP {exc.code}). Ask me in chat to check access and retry."
            if exc.code in (401, 403):
                return self.waiting(message)
        except (OSError, ValueError, KeyError, TypeError) as exc:
            reason = str(exc.reason) if isinstance(exc, urllib.error.URLError) else str(exc)
            self.log(f"{self.step.value}: {type(exc).__name__}: {reason}")
            message = "Setup could not finish. Ask me in chat to check the installation log and retry."
        return self.failed(message)


def main() -> None:
    from packs.powerset.primitives.install.workflow import SourceOnboarding, _parser
    from packs.powerset.primitives.install.pipeline import ProcessingOnboarding
    from packs.ingestion.primitives.deep_context.review.cli import start_server

    parser = argparse.ArgumentParser(description=__doc__, parents=[_parser(add_help=False)])
    parser.add_argument("--root", type=Path, default=Path.cwd())
    parser.add_argument("--harness", choices=("codex", "claude-code", "pi"), action="append")
    parser.add_argument("--port", type=int)
    parser.add_argument("--approve-spend", choices=("synthesize", "cluster", "enrich", "index"),
                        action="append", default=[])
    parser.add_argument("--approve-upload", action="store_true")
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
            status.write(step=InstallStep.RUNTIME, status=InstallState.FAILED,
                         message="The progress page could not start. I can check the log and retry.",
                         pid=0, retry_command=flow.retry_command)
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
            payload = ProcessingOnboarding(root, approved_spend=tuple(args.approve_spend),
                                           approve_upload=args.approve_upload, port=port).run()
    prefix = {"completed": "DONE", "waiting": "NEEDS YOU", "running": "NEEDS YOU", "failed": "FAILED"}
    message = (payload.get("action") or {}).get("text") or payload.get("message", "Setup stopped")
    print(f"{prefix[payload['status']]}: {message}", flush=True)
    raise SystemExit({"completed": 0, "waiting": NEEDS_YOU, "running": NEEDS_YOU, "failed": 1}[payload["status"]])


if __name__ == "__main__":
    main()
