#!/usr/bin/python3

import dataclasses
from contextvars import ContextVar

import apprise

from .config import cfgmgr_ctx

# holds the apprise object context so it can be used across modules without circular imports
apobj_ctx: ContextVar[apprise.Apprise | None] = ContextVar("apobj", default=None)


@dataclasses.dataclass
class NotificationManager:
    apobj: apprise.Apprise = dataclasses.field(default_factory=apprise.Apprise)

    def __post_init__(self):
        if apobj_ctx.get(None):
            raise RuntimeError("Notification manager already exists in current context")
        apobj_ctx.set(self.apobj)

    async def run(self) -> None:
        cfgmgr = cfgmgr_ctx.get(None)
        if not cfgmgr:
            raise RuntimeError("Configuration manager is unavailable in current context")
        modified_flag = cfgmgr.get_modified_flag()

        while True:
            # the only thing this manager does is rebuild the apprise object on config change
            await modified_flag.wait()
            modified_flag.clear()
            assert cfgmgr.config

            self.apobj.clear()

            for notifier in cfgmgr.config.notifications:
                self.apobj.add(notifier.url, tag=notifier.tags)
