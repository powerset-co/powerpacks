"""The install steps a setup run moves through, and the states a step can be in."""
from __future__ import annotations

from enum import Enum


class InstallState(str, Enum):
    RUNNING = "running"
    WAITING = "waiting"
    FAILED = "failed"
    COMPLETED = "completed"
    SKIPPED = "skipped"


class InstallStep(str, Enum):
    RUNTIME = "runtime"
    DEPENDENCIES = "dependencies"
    SKILLS = "skills"
    ACCOUNT = "account"
    CREDENTIALS = "credentials"
    CONNECTION = "connection"
    NETWORK = "network"
    SOURCES = "sources"
    LINKEDIN_LOGIN = "linkedin_login"
    GMAIL_TOOLS = "gmail_tools"
    GMAIL_LOGIN = "gmail_login"
    GMAIL_SYNC = "gmail_sync"
    GMAIL_IMPORT = "gmail_import"
    IMESSAGE_ACCESS = "imessage_access"
    IMESSAGE_IMPORT = "imessage_import"
    WHATSAPP_TOOLS = "whatsapp_tools"
    WHATSAPP_LOGIN = "whatsapp_login"
    WHATSAPP_SYNC = "whatsapp_sync"
    WHATSAPP_IMPORT = "whatsapp_import"
    LINKEDIN = "linkedin"
    DEEP_CONTEXT = "deep_context"
    ENRICH = "enrich"
    INDEX = "index"
    VALIDATE = "validate"
    READY = "ready"


DEFAULT_PLAN = ["runtime", "dependencies", "skills", "account", "credentials", "connection", "network"]
PROCESSING_STEPS = (InstallStep.DEEP_CONTEXT, InstallStep.ENRICH, InstallStep.INDEX, InstallStep.VALIDATE,
                    InstallStep.READY)
