"""Every word the install status page says, in the order a setup run says it.

An event names one moment of a run. `InstallStatus.write(event, ...)` looks it
up here, fills its {placeholders}, and writes the line, the note and the state
to the install manifest, which the page, the agent and the CLI read. Code never
words the page itself: it names an event, and a primitive's own text goes to the
manifest's details and the log, never to the page.

Read it top to bottom as the script; `bin/status-prose` prints it and
`bin/status-prose play` plays it on the status page.

Not worded here:
  - what the page says while it cannot reach setup (`OFFLINE` in InstallPage.tsx);
  - the live enrichment line ("Looking up 3 people"), the review page's own copy
    (`web/src/pages/review/enrich/copy.ts`);
  - the live index line ("Building search records"), sent by the Modal run.

Changelog:
  2026-10-05: created; the copy moved here from status.py, workflow.py,
      pipeline.py, onboard.py, bin/bootstrap, the LinkedIn reader and the page.
"""
from __future__ import annotations

from dataclasses import dataclass

from packs.powerset.primitives.install.steps import InstallState, InstallStep

R, W, F, C, S = InstallState.RUNNING, InstallState.WAITING, InstallState.FAILED, InstallState.COMPLETED, InstallState.SKIPPED


@dataclass(frozen=True)
class Prose:
    """One event: the step it belongs to (None: the step the run is on), the state it
    puts that step in, the line under the page title, the note under the line, and
    what the page offers for it (`action`: qr, permission, gmail, review, ...)."""
    step: InstallStep | None
    state: InstallState
    line: str
    note: str = ""
    action: str = ""


@dataclass(frozen=True)
class Row:
    """One row of the page's step list and the steps it covers."""
    label: str
    steps: tuple[InstallStep, ...]
    done_label: str = ""
    needs: InstallStep | None = None  # shown only when this step is planned


# The page's rows, top to bottom. Setup takes every login first, then each source's sync.
ROWS = (
    Row("Installing Powerpacks", (InstallStep.RUNTIME, InstallStep.DEPENDENCIES, InstallStep.SKILLS,
                                  InstallStep.ACCOUNT, InstallStep.CREDENTIALS, InstallStep.CONNECTION,
                                  InstallStep.NETWORK)),
    Row("Logging in to your accounts", (InstallStep.LINKEDIN_LOGIN, InstallStep.GMAIL_TOOLS, InstallStep.GMAIL_LOGIN,
                                        InstallStep.IMESSAGE_ACCESS, InstallStep.WHATSAPP_TOOLS,
                                        InstallStep.WHATSAPP_LOGIN)),
    Row("Syncing LinkedIn", (InstallStep.LINKEDIN,)),
    Row("Syncing Gmail", (InstallStep.GMAIL_SYNC, InstallStep.GMAIL_IMPORT)),
    Row("Syncing iMessage", (InstallStep.IMESSAGE_IMPORT,)),
    Row("Syncing WhatsApp", (InstallStep.WHATSAPP_SYNC, InstallStep.WHATSAPP_IMPORT)),
    Row("Discovering your contacts", (InstallStep.DEEP_CONTEXT,)),
    Row("Enriching your contacts", (InstallStep.ENRICH,)),
    Row("Waiting for your review", (InstallStep.REVIEW,), done_label="Review completed"),
    # A run whose every source is skipped ends ready without building an index.
    Row("Building your search index", (InstallStep.INDEX, InstallStep.VALIDATE, InstallStep.READY),
        needs=InstallStep.INDEX),
)

# The page's fixed words: the title over each situation, the row states, buttons.
PAGE = {
    "title.running": "Setting up Powerpacks",
    "title.waiting": "One thing to finish",
    "title.failed": "Setup needs a fix",
    "title.completed": "Powerpacks is installed",
    "title.ready": "Powerpacks is ready",
    "title.paused": "Setup paused",
    "title.signing_in": "Waiting for you to sign in",
    "title.review": "Waiting for your review",
    "title.processing": "Your contacts are saved",
    "state.running": "Working",
    "state.waiting": "Waiting",
    "state.failed": "Needs a fix",
    "state.completed": "Done",
    "state.skipped": "Skipped",
    "state.paused": "Paused",
    "state.next": "Next",
    "failed.note": "Your progress is saved. I can check this step and retry.",
    "history.folded": "{count} tasks completed",
    "history.open": "Hide completed tasks",
    "permission.button": "Open settings & show the app",
    "qr.loading": "Getting your QR code…",
    "qr.where": "WhatsApp → Settings → Linked devices → Link a device",
    "qr.alt": "Scan this QR code to link WhatsApp",
    "review.offer.one": "1 LinkedIn match needs a quick look when you have time.",
    "review.offer.many": "{count} LinkedIn matches need a quick look when you have time.",
    "review.button": "Review contacts",
}

_QR_NOTE = "Scan the code with WhatsApp. I’ll continue automatically."
_GMAIL_NOTE = "Finish connecting Gmail in your browser. I’ll continue here."
_WHATSAPP_NOTE = "FYI you will see lots of notifications on your phone for WhatsApp syncing. This is expected."

# Every event a run writes, in the order a run writes them.
PROSE: dict[str, Prose] = {
    # Installing Powerpacks (bin/bootstrap)
    "install.starting": Prose(InstallStep.RUNTIME, W, "Starting Powerpacks"),
    "install.preparing_mac": Prose(InstallStep.RUNTIME, R, "Preparing your Mac"),
    "install.dependencies": Prose(InstallStep.DEPENDENCIES, R, "Installing what Powerpacks needs"),
    "install.dependencies_ready": Prose(InstallStep.DEPENDENCIES, C, "Powerpacks dependencies are ready"),
    "install.adding_skills": Prose(InstallStep.SKILLS, R, "Adding Powerpacks skills"),
    "install.skills_ready": Prose(InstallStep.SKILLS, C, "Powerpacks skills are installed"),
    "install.local_only": Prose(None, S, "Skipped for this local-only installation"),
    "install.local_only_done": Prose(InstallStep.READY, C, "Powerpacks skills are installed. No account was connected."),
    "install.stopped": Prose(None, F, "Installation stopped. I'm checking what happened."),
    "install.page_failed": Prose(InstallStep.RUNTIME, F, "The progress page could not start. I can check the log and retry."),
    "install.unreadable": Prose(InstallStep.RUNTIME, F, "Installation status could not be read. I'm checking the installation log."),

    # Installing Powerpacks: the Powerset account (onboard.py)
    "account.signing_in": Prose(InstallStep.ACCOUNT, W, "Waiting for account login. Sign in in the browser; setup will continue automatically."),
    "account.link_expired": Prose(InstallStep.ACCOUNT, W, "The sign-in link expired. Opening a fresh one."),
    "account.checking": Prose(InstallStep.ACCOUNT, R, "Checking your account"),
    "account.connected": Prose(InstallStep.ACCOUNT, C, "Connected as {email}"),
    "account.reused": Prose(InstallStep.ACCOUNT, S, "Already signed in as {email}. Tell me if that's the wrong account."),
    "account.login_failed": Prose(InstallStep.ACCOUNT, F, "Account login did not finish. Tell me in chat to try again; I will reopen the sign-in page."),
    "credentials.preparing": Prose(InstallStep.CREDENTIALS, R, "Preparing search access"),
    "credentials.ready": Prose(InstallStep.CREDENTIALS, C, "Search access is ready"),
    "credentials.not_provisioned": Prose(InstallStep.CREDENTIALS, W, "Search access isn't set up for {email} yet.", note="Ask Powerset to finish enabling search for this account, then tell me to retry."),
    "credentials.hosted_off": Prose(InstallStep.CREDENTIALS, S, "Hosted search isn't enabled for {email} yet, so local setup continues. Tell me if you want to sign in with a different account."),
    "connection.connecting": Prose(InstallStep.CONNECTION, R, "Connecting Powerpacks to your agent"),
    "connection.done": Prose(InstallStep.CONNECTION, C, "Agent connection configured"),
    "connection.skipped": Prose(InstallStep.CONNECTION, S, "Agent connection was skipped. Checking direct Powerset access next."),
    "network.checking": Prose(InstallStep.NETWORK, R, "Checking your network"),
    "network.checking_one": Prose(InstallStep.NETWORK, R, "Checking {network} for {email}"),
    "network.all_empty": Prose(InstallStep.NETWORK, W, "Every network for {email} has 0 people.", note="Tell me in chat whether to switch accounts or connect your contacts."),
    "network.unconfirmed": Prose(InstallStep.NETWORK, W, "Your network for {email} could not be confirmed.", note="Tell me in chat whether to switch accounts or choose a network."),
    "network.empty": Prose(InstallStep.NETWORK, W, "{network} for {email} has 0 people.", note="Tell me in chat whether to switch accounts or connect your contacts."),
    "network.not_searchable": Prose(InstallStep.NETWORK, W, "{network} has {count:,} people, but its searchable profiles are not ready.", note="Ask me in chat to check the network before searching."),
    "network.ready": Prose(InstallStep.NETWORK, C, "{network} for {email} is ready: {count:,} people in this network."),
    "network.ready_small": Prose(InstallStep.NETWORK, C, "{network} for {email} is ready: {count:,} people in this network. This is a small network; you may want another network or account."),
    "account.denied": Prose(None, W, "Powerset could not complete this check (HTTP {code}). Ask me in chat to check access and retry."),
    "account.service_failed": Prose(None, F, "Powerset could not complete this check (HTTP {code}). Ask me in chat to check access and retry."),
    "account.error": Prose(None, F, "Setup could not finish. Ask me in chat to check the installation log and retry."),

    # Choosing sources (workflow.py)
    "sources.selected": Prose(InstallStep.SOURCES, C, "Sources selected"),
    "source.skipped": Prose(None, S, "Skipped for now"),
    "source.not_started": Prose(None, W, "Not started"),
    "sources.all_skipped": Prose(InstallStep.READY, C, "Powerpacks is installed"),
    "gmail.which_accounts": Prose(InstallStep.GMAIL_LOGIN, W, "Which Gmail accounts should I add? The first one owns the Gmail setup.", action="gmail"),

    # Logging in to your accounts: tools first, then every login back to back
    "tools.preparing": Prose(None, R, "Preparing import tools"),
    "tools.ready": Prose(None, C, "Contact import tools are ready"),
    "tools.needs_password": Prose(None, W, "Your Mac password is needed to prepare import tools. I'll ask in chat."),
    "tools.failed": Prose(None, F, "Import tools could not be prepared. I'm checking what happened."),
    "linkedin.login.current": Prose(InstallStep.LINKEDIN_LOGIN, C, "LinkedIn connections ready"),
    "linkedin.login.checking": Prose(InstallStep.LINKEDIN_LOGIN, R, "Checking LinkedIn. Log in to LinkedIn in the window that opens if it asks."),
    "linkedin.login.waiting": Prose(InstallStep.LINKEDIN_LOGIN, W, "Log in to LinkedIn in the Chrome window Powerpacks opened."),
    "linkedin.login.done": Prose(InstallStep.LINKEDIN_LOGIN, C, "Signed in to LinkedIn"),
    "linkedin.login.failed": Prose(InstallStep.LINKEDIN_LOGIN, F, "LinkedIn sign-in stopped. I'm checking what happened."),
    "gmail.checking": Prose(InstallStep.GMAIL_LOGIN, R, "Checking Gmail access"),
    "gmail.app.starting": Prose(InstallStep.GMAIL_LOGIN, R, "Creating your Gmail app in Google Cloud. Sign in to Google in your browser if it asks."),
    "gmail.app.sign_in": Prose(InstallStep.GMAIL_LOGIN, R, "Sign in to Google in the Chrome window that opened. Setup continues on its own after."),
    "gmail.app.naming": Prose(InstallStep.GMAIL_LOGIN, R, "Creating your Gmail app in Google Cloud: naming the app"),
    "gmail.app.permissions": Prose(InstallStep.GMAIL_LOGIN, R, "Creating your Gmail app in Google Cloud: adding Gmail read access"),
    "gmail.app.client": Prose(InstallStep.GMAIL_LOGIN, R, "Creating your Gmail app in Google Cloud: creating its sign-in key"),
    "gmail.app.ready": Prose(InstallStep.GMAIL_LOGIN, R, "Your Gmail app is ready in Google Cloud"),
    "gmail.app.stopped": Prose(InstallStep.GMAIL_LOGIN, W, "Gmail setup stopped in Google Cloud. I'm looking into it.", action="gmail"),
    "gmail.app.failed": Prose(InstallStep.GMAIL_LOGIN, F, "Gmail setup could not finish. I'm checking what happened."),
    "gmail.allowing": Prose(InstallStep.GMAIL_LOGIN, R, "Allowing your Gmail accounts in your Gmail app"),
    "gmail.allowed": Prose(InstallStep.GMAIL_LOGIN, R, "Your Gmail accounts are allowed"),
    "gmail.allowing.failed": Prose(InstallStep.GMAIL_LOGIN, F, "Your Gmail accounts could not be allowed. I'm checking what happened."),
    "gmail.connect": Prose(InstallStep.GMAIL_LOGIN, R, "Connecting {email}"),
    "gmail.connect.waiting": Prose(InstallStep.GMAIL_LOGIN, W, "Connect Gmail in your browser", note=_GMAIL_NOTE, action="gmail"),
    "gmail.connect.failed": Prose(InstallStep.GMAIL_LOGIN, F, "Gmail could not be connected. I'm checking what happened."),
    "gmail.connected": Prose(InstallStep.GMAIL_LOGIN, C, "Gmail is connected"),
    "imessage.checking": Prose(InstallStep.IMESSAGE_ACCESS, R, "Checking Messages access"),
    "imessage.permission": Prose(InstallStep.IMESSAGE_ACCESS, W, "Allow access to Messages",
                                 note="Powerpacks reads your iMessage history to find the people you talk to. Drag {app} into "
                                      "Full Disk Access and turn it on; I’ll continue automatically.",
                                 action="permission"),
    "imessage.allowed": Prose(InstallStep.IMESSAGE_ACCESS, C, "Messages access is allowed"),
    "imessage.unavailable": Prose(InstallStep.IMESSAGE_ACCESS, F, "Messages could not be read on this Mac. I'm checking why."),
    "whatsapp.checking": Prose(InstallStep.WHATSAPP_LOGIN, R, "Checking WhatsApp"),
    "whatsapp.qr": Prose(InstallStep.WHATSAPP_LOGIN, W, "Connect WhatsApp", note=_QR_NOTE, action="qr"),
    "whatsapp.qr.refreshed": Prose(InstallStep.WHATSAPP_LOGIN, W, "Refreshing your WhatsApp QR code", note=_QR_NOTE, action="qr"),
    "whatsapp.already_linked": Prose(InstallStep.WHATSAPP_LOGIN, C, "WhatsApp is linked"),
    "whatsapp.linked": Prose(InstallStep.WHATSAPP_LOGIN, C, "WhatsApp is linked. Your message history is downloading in the background.", note=_WHATSAPP_NOTE),
    "whatsapp.blocked": Prose(InstallStep.WHATSAPP_LOGIN, W, "WhatsApp can't link right now. I'll explain in chat."),
    "whatsapp.failed": Prose(InstallStep.WHATSAPP_LOGIN, F, "WhatsApp could not be linked. I'm checking what happened."),

    # Syncing, one source at a time
    "linkedin.sync.current": Prose(InstallStep.LINKEDIN, C, "LinkedIn connections ready"),
    "linkedin.reading": Prose(InstallStep.LINKEDIN, R, "Reading your LinkedIn connections"),
    "linkedin.reading.count": Prose(InstallStep.LINKEDIN, R, "Reading your LinkedIn connections: {read:,} of {total:,}"),
    "linkedin.done.read": Prose(InstallStep.LINKEDIN, C, "{connections:,} LinkedIn connections ({added:,} new)"),
    "linkedin.done.partial": Prose(InstallStep.LINKEDIN, C, "{connections:,} LinkedIn connections ({added:,} new); LinkedIn shows {total:,}"),
    "linkedin.done.limit": Prose(InstallStep.LINKEDIN, C, "{connections:,} LinkedIn connections ({added:,} new). The rest keep syncing on your next run; your contacts are ready to process now."),
    "linkedin.done.stalled": Prose(InstallStep.LINKEDIN, C, "LinkedIn stopped sending connections after {read:,} of {total:,}, so I stopped to keep your account safe.", note="The rest sync on your next run. Your contacts are ready to process now."),
    "linkedin.done.export_requested": Prose(InstallStep.LINKEDIN, C, "LinkedIn stopped sending connections after {read:,} of {total:,}, so I stopped to keep your account safe and asked LinkedIn for your data export.", note="It can take a day; the next setup run imports it. Your contacts are ready to process now."),
    "linkedin.done.export_imported": Prose(InstallStep.LINKEDIN, C, "{connections:,} LinkedIn connections ({added:,} new, from your LinkedIn data export)"),
    "linkedin.waiting": Prose(InstallStep.LINKEDIN, W, "Log in to LinkedIn in the Chrome window Powerpacks opened."),
    "linkedin.failed": Prose(InstallStep.LINKEDIN, F, "Your LinkedIn connections could not be read. I'm checking what happened."),
    "gmail.sync.current": Prose(InstallStep.GMAIL_SYNC, C, "Gmail is already imported"),
    "gmail.import.current": Prose(InstallStep.GMAIL_IMPORT, C, "Gmail contacts ready"),
    "gmail.syncing": Prose(InstallStep.GMAIL_SYNC, R, "Syncing Gmail"),
    "gmail.synced": Prose(InstallStep.GMAIL_SYNC, C, "Gmail is synced"),
    "gmail.sync.waiting": Prose(InstallStep.GMAIL_SYNC, W, "Gmail needs you before it can sync. I'll explain in chat."),
    "gmail.sync.failed": Prose(InstallStep.GMAIL_SYNC, F, "Gmail could not sync. I'm checking what happened."),
    "gmail.importing": Prose(InstallStep.GMAIL_IMPORT, R, "Adding Gmail contacts"),
    "gmail.imported": Prose(InstallStep.GMAIL_IMPORT, C, "Gmail contacts added"),
    "gmail.import.failed": Prose(InstallStep.GMAIL_IMPORT, F, "Gmail contacts could not be added. I'm checking what happened."),
    "imessage.current": Prose(InstallStep.IMESSAGE_IMPORT, C, "iMessage contacts ready"),
    "imessage.reading": Prose(InstallStep.IMESSAGE_IMPORT, R, "Reading Messages"),
    "imessage.importing": Prose(InstallStep.IMESSAGE_IMPORT, R, "Adding iMessage contacts"),
    "imessage.imported": Prose(InstallStep.IMESSAGE_IMPORT, C, "iMessage contacts added"),
    "imessage.read.failed": Prose(InstallStep.IMESSAGE_IMPORT, F, "Messages could not be read. I'm checking what happened."),
    "imessage.import.failed": Prose(InstallStep.IMESSAGE_IMPORT, F, "iMessage contacts could not be added. I'm checking what happened."),
    "whatsapp.sync.current": Prose(InstallStep.WHATSAPP_SYNC, C, "WhatsApp contacts already imported"),
    "whatsapp.import.current": Prose(InstallStep.WHATSAPP_IMPORT, C, "WhatsApp contacts ready"),
    "whatsapp.downloading": Prose(InstallStep.WHATSAPP_SYNC, R, "Downloading your WhatsApp history. The first sync takes 30 minutes to a few hours.", note=_WHATSAPP_NOTE),
    "whatsapp.downloading.count": Prose(InstallStep.WHATSAPP_SYNC, R, "Downloading your WhatsApp history: {messages:,} messages so far. The first sync takes 30 minutes to a few hours.", note=_WHATSAPP_NOTE),
    "whatsapp.syncing": Prose(InstallStep.WHATSAPP_SYNC, R, "Syncing WhatsApp and fetching older messages in short chats", note=_WHATSAPP_NOTE),
    "whatsapp.synced": Prose(InstallStep.WHATSAPP_SYNC, C, "WhatsApp is synced"),
    "whatsapp.sync.waiting": Prose(InstallStep.WHATSAPP_SYNC, W, "WhatsApp needs you before it can sync. I'll explain in chat."),
    "whatsapp.sync.failed": Prose(InstallStep.WHATSAPP_SYNC, F, "WhatsApp could not sync. I'm checking what happened."),
    "whatsapp.importing": Prose(InstallStep.WHATSAPP_IMPORT, R, "Adding WhatsApp contacts"),
    "whatsapp.imported": Prose(InstallStep.WHATSAPP_IMPORT, C, "WhatsApp contacts added"),
    "whatsapp.import.failed": Prose(InstallStep.WHATSAPP_IMPORT, F, "WhatsApp contacts could not be added. I'm checking what happened."),
    "sources.ready": Prose(InstallStep.DEEP_CONTEXT, W, "{counts}", action="processing"),  # see source_counts

    # Discovering your contacts (pipeline.py)
    "discover.linkedin": Prose(InstallStep.DEEP_CONTEXT, R, "Adding your LinkedIn connections"),
    "discover.merging": Prose(InstallStep.DEEP_CONTEXT, R, "Preparing your contacts"),
    "discover.people": Prose(InstallStep.DEEP_CONTEXT, R, "Discovering your contacts"),
    "discover.checking": Prose(InstallStep.DEEP_CONTEXT, R, "Checking your contacts"),
    "discover.reusing": Prose(InstallStep.DEEP_CONTEXT, R, "Reusing your previous context"),
    "discover.owner_needed": Prose(InstallStep.DEEP_CONTEXT, W, "Add your LinkedIn profile to continue. I'll ask in chat.", action="owner"),
    "discover.owner": Prose(InstallStep.DEEP_CONTEXT, R, "Preparing your profile"),
    "discover.reading": Prose(InstallStep.DEEP_CONTEXT, R, "Reading your messages"),
    "discover.estimating": Prose(InstallStep.DEEP_CONTEXT, R, "Estimating context processing"),
    "discover.learning": Prose(InstallStep.DEEP_CONTEXT, R, "Learning about your contacts"),
    "discover.composing": Prose(InstallStep.DEEP_CONTEXT, R, "Writing what you know about each contact"),
    "discover.validating": Prose(InstallStep.DEEP_CONTEXT, R, "Checking your contact context"),
    "discover.duplicates": Prose(InstallStep.DEEP_CONTEXT, R, "Checking duplicate contacts"),
    "discover.combining": Prose(InstallStep.DEEP_CONTEXT, R, "Combining duplicate contacts"),
    "discover.grouping": Prose(InstallStep.DEEP_CONTEXT, R, "Grouping each person's contacts"),
    "discover.done": Prose(InstallStep.DEEP_CONTEXT, C, "Your contacts are ready"),

    # Enriching your contacts
    "enrich.running": Prose(InstallStep.ENRICH, R, "Enriching your contacts"),
    "enrich.done": Prose(InstallStep.ENRICH, C, "Your contacts are enriched"),
    "enrich.deferred": Prose(InstallStep.ENRICH, S, "Research and LinkedIn matching didn't finish. Search is built without it; the next setup run tries again."),
    "profiles.deferred": Prose(InstallStep.ENRICH, S, "LinkedIn profile lookups didn't finish. Search is built without them; the next setup run tries again."),

    # Building your search index
    "index.preparing": Prose(InstallStep.INDEX, R, "Preparing your search index"),
    "index.profiles": Prose(InstallStep.INDEX, R, "Looking up LinkedIn profiles"),
    "index.estimating": Prose(InstallStep.INDEX, R, "Estimating search indexing"),
    "index.building": Prose(InstallStep.INDEX, R, "Building your search index"),
    "index.resuming": Prose(InstallStep.INDEX, R, "Resuming your search index"),
    "index.recovery": Prose(InstallStep.INDEX, W, "An earlier index needs checking before another upload. I'm checking it.", action="recovery"),
    "index.done": Prose(InstallStep.INDEX, C, "Your search index is built"),
    "validate.checking": Prose(InstallStep.VALIDATE, R, "Checking your search"),
    "validate.done": Prose(InstallStep.VALIDATE, C, "Search is ready: {people:,} people searchable."),
    "search.ready": Prose(InstallStep.READY, C, "Search is ready: {people:,} people searchable.", note="{follow_ups}"),

    # Any step
    "spend.approval": Prose(None, W, "Approve the estimated processing cost to continue. I'll ask in chat.", action="approval"),
    "step.waiting": Prose(None, W, "This step needs you. I'll explain in chat.", action="error"),
    "step.failed": Prose(None, F, "This step stopped. I'm checking what happened.", action="error"),
    "setup.paused": Prose(None, W, "Setup paused. I can resume it from here.", action="resume"),
}


_SOURCE_COUNT = "{source}: {count:,} contacts"
_NO_SOURCE_COUNTS = "Sources are ready"


def source_counts(counts: dict[str, int]) -> str:
    """The contacts each source added, for the line once every source is in."""
    return " · ".join(_SOURCE_COUNT.format(source=source.title(), count=count)
                      for source, count in counts.items()) or _NO_SOURCE_COUNTS


def render(event: str, values: dict) -> tuple[Prose, str, str]:
    """The event's prose with its line and note filled from `values`."""
    prose = PROSE[event]
    return prose, prose.line.format(**values), prose.note.format(**values)


def page_prose() -> dict:
    """The page's rows and fixed words, sent with every status read."""
    return {"rows": [{"label": row.label, "steps": [step.value for step in row.steps],
                      "done_label": row.done_label, "needs": row.needs.value if row.needs else ""} for row in ROWS],
            "page": PAGE}
