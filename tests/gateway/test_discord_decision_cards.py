"""Discord Decision Card button view tests."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from gateway import decision_cards as dc
from plugins.platforms.discord.adapter import DecisionCardView


def _make_interaction(*, user_id: str = "42", display_name: str = "Chris"):
    embed = SimpleNamespace(color=None, footer=None)

    def set_footer(*, text=None, **_):
        embed.footer = {"text": text}
        return embed

    embed.set_footer = set_footer
    message = SimpleNamespace(id="m1", embeds=[embed])
    return SimpleNamespace(
        user=SimpleNamespace(id=user_id, display_name=display_name, roles=[]),
        channel_id="c1",
        message=message,
        response=SimpleNamespace(
            send_message=AsyncMock(),
            edit_message=AsyncMock(),
        ),
    )


@pytest.fixture
def queue_db(tmp_path, monkeypatch):
    path = tmp_path / "decision_cards.db"
    monkeypatch.setenv("HERMES_DECISION_QUEUE_DB", str(path))
    return path


def _create_card():
    return dc.create_card(
        question="Should Dante continue with the pilot branch?",
        context="The queue helper is implemented.\nThe gateway primitive is staged.",
        default_action="Wait until tomorrow morning",
        fire_at="2026-07-03 07:00 CT",
        requested_by="Dante morning pulse",
        source_ref="kanban:t_demo",
        originating_profile="dante",
    )


def test_decision_card_view_renders_four_contract_buttons(queue_db):
    card = _create_card()
    view = DecisionCardView(card_id=card.id, allowed_user_ids={"42"})

    assert [getattr(child, "label") for child in view.children] == [
        "Yes",
        "No",
        "Wait",
        "Need More Info",
    ]
    assert [getattr(child, "custom_id") for child in view.children] == [
        f"decision:{card.id}:yes",
        f"decision:{card.id}:no",
        f"decision:{card.id}:wait",
        f"decision:{card.id}:info",
    ]


@pytest.mark.asyncio
async def test_yes_button_updates_queue_and_disables_card(queue_db):
    card = _create_card()
    view = DecisionCardView(card_id=card.id, allowed_user_ids={"42"})
    interaction = _make_interaction()

    await view._resolve_action(interaction, "yes")

    updated = dc.get_card(card.id)
    assert updated.status == "answered_yes"
    assert updated.answer == "yes"
    assert updated.answered_by == "Chris"
    assert all(getattr(child, "disabled") for child in view.children)
    interaction.response.edit_message.assert_awaited_once()
    interaction.response.send_message.assert_not_awaited()


@pytest.mark.asyncio
async def test_info_button_posts_plain_explanation_without_disabling(queue_db):
    card = dc.create_card(
        question="Should Dante continue with the pilot branch?",
        context="The queue helper is implemented.",
        default_action="Wait until tomorrow morning",
        fire_at="2026-07-03 07:00 CT",
        requested_by="Dante morning pulse",
        originating_profile="dante",
        explanation="This is the plain explanation.",
    )
    view = DecisionCardView(card_id=card.id, allowed_user_ids={"42"})
    interaction = _make_interaction()

    await view._resolve_action(interaction, "info")

    updated = dc.get_card(card.id)
    assert updated.status == "info_requested"
    assert not any(getattr(child, "disabled") for child in view.children)
    interaction.response.send_message.assert_awaited_once_with(
        "This is the plain explanation.",
        ephemeral=False,
    )


@pytest.mark.asyncio
async def test_unauthorized_click_is_rejected(queue_db):
    card = _create_card()
    view = DecisionCardView(card_id=card.id, allowed_user_ids={"999"})
    interaction = _make_interaction(user_id="42")

    await view._resolve_action(interaction, "yes")

    assert dc.get_card(card.id).status == "pending"
    interaction.response.send_message.assert_awaited_once()
    kwargs = interaction.response.send_message.call_args.kwargs
    assert kwargs.get("ephemeral") is True


def test_decision_card_requires_originating_profile(queue_db):
    with pytest.raises(dc.DecisionCardError, match="originating_profile is required"):
        dc.create_card(
            question="Should Virgil continue with the smoke card?",
            context="The card must be attributable to the asking agent.",
            default_action="Do nothing",
            fire_at="2026-07-04 22:00 CT",
            requested_by="Virgil smoke test",
        )


def test_decision_card_text_leads_with_asking_agent(queue_db):
    card = dc.create_card(
        question="Should Virgil continue with the smoke card?",
        context="The card must be visibly attributable.",
        default_action="Do nothing",
        fire_at="2026-07-04 22:00 CT",
        requested_by="Virgil smoke test",
        source_ref="kanban:t_demo",
        originating_profile="virgil",
    )

    assert dc.format_card_text(card).splitlines()[0] == "**Virgil asks:**"
