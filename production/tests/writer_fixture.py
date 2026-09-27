"""Test shot cards carry the writer's whole prompt, as a real agent's do.

An agent fetches the manuals (`cli playbook`), writes the whole prompt from them (owner 2026-09-26: the writer
writes every section; the platform only adds what production/prompt.py allows), and names the manuals version and
what the card version changes. `authored` does the same for a fixture card.
"""
import re
from typing import Any

from production import playbook
from production.prompt import TAG


def took_manuals(store: Any, pid: str, actor: Any) -> None:
    store.append_event(pid, playbook.FETCHED, {'version': playbook.version(), 'actor_id': actor.actor_id,
                                               'credential_id': actor.credential_id, 'role': actor.role})


def card_tags(card: dict[str, Any]) -> list[str]:
    """The @tags the card lists for the people, props and location in frame."""
    material = card.get('The material', {})
    listed = [str(t).lstrip('@') for t in [*material.get('everyone in frame with their tags and state variants', []),
                                           *material.get('props and vehicles with tags', [])]]
    place = str(material.get('the location and INT/EXT with the asset that covers it') or '')
    return list(dict.fromkeys([*listed, *(m[1] for m in TAG.finditer(place))]))


def prompt(card: dict[str, Any], *, acting: str | None = None) -> str:
    """A whole CINEDANCE-shaped prompt: bare reference lines (the platform pastes the registry descriptors), then
    the writer's own blocks."""
    material, direction = card.get('The material', {}), card.get('Direction', {})
    action = str(material.get('the action in one to three sentences') or 'The subject crosses the frame.').strip()
    end = str(direction.get('end state') or 'The subject holds its final position.').strip()
    seconds = float(material.get('the running time in seconds') or 4)
    blocks = ['ACTIVE REFERENCES\n' + '\n'.join('@' + tag for tag in card_tags(card)),
              f'FIRST FRAME AND SPATIAL BLOCKING\nFrame one holds the setting before anything moves. {action.split(".")[0]} is about to begin.',
              'OPTICS\nEye-level wide view, 60° field of view for the whole shot; verticals stay vertical.',
              'CAMERA\nThe camera stays locked off at real-time speed.',
              f'ACTION TIMING\n0.0–{seconds:.1f}s — {action}\nThis shot ends with: {end}']
    if acting:
        blocks.append('CHARACTER ACTING\n' + acting)
    return re.sub(r'ACTIVE REFERENCES\n\n', '', '\n\n'.join(blocks))


def authored(card: dict[str, Any], *, store: Any, pid: str, actor: Any, note: str = 'First version of this card.',
             acting: str | None = None) -> dict[str, Any]:
    took_manuals(store, pid, actor)
    return {'prompt': prompt(card, acting=acting), 'playbook_version': playbook.version(), 'change_note': note}
