"""PPTX renderer (WORKPLAN C7). Owner: Agent B.

``render_deck`` turns a ``Deck`` into a themed 16:9 .pptx (python-pptx), one
builder per slide layout. It only reads images from the local media store
(``media.resolve_media``) and never touches the network. Sync and CPU-bound:
call it via ``asyncio.to_thread`` from async code.
"""

from app.schemas.deck import Deck

PPTX_MIME = "application/vnd.openxmlformats-officedocument.presentationml.presentation"


def render_deck(deck: Deck) -> bytes:
    raise NotImplementedError
