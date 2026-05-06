from .architect import ARCHITECT
from .coder import CODER
from .config import BotConfig
from .explorer import EXPLORER
from .pm import PM
from .reviewer import REVIEWER

_BOTS = {
    b.identity: b
    for b in [ARCHITECT, REVIEWER, PM, EXPLORER, CODER]
}


def get_bot(identity: str) -> BotConfig:
    bot = _BOTS.get(identity)
    if bot is None:
        raise ValueError(f"Unknown bot identity: {identity}")
    return bot


def list_bots() -> list[BotConfig]:
    return list(_BOTS.values())
