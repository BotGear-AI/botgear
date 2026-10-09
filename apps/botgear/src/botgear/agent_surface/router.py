# unmanaged
"""FastToolRouter — System One tool catalog candidate router for agent_surface.

Uses rlx_decision (Jev / System One models) to evaluate user utterances against
available service actions, rapidly filtering large catalogs (100+ actions)
down to the top candidate tools in sub-100ms.
"""
from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional

from botgear.agent_surface.models import ToolDescriptor
from rlx_decision import Choice, DecisionClient, create_decision_client

logger = logging.getLogger(__name__)


class FastToolRouter:
    """Pre-filters tool catalogs down to relevant actions before System 2 LLM inference."""

    def __init__(
        self,
        client: Optional[DecisionClient] = None,
        max_candidates: int = 5,
    ):
        self.client = client or create_decision_client()
        self.max_candidates = max_candidates

    async def route_tools(
        self,
        query: str,
        catalog: Dict[str, ToolDescriptor],
    ) -> List[ToolDescriptor]:
        """Given a query and catalog, return the most relevant ToolDescriptors."""
        if not catalog or not query:
            return list(catalog.values())

        if len(catalog) <= self.max_candidates:
            return list(catalog.values())

        # Build candidate options
        options = []
        name_to_desc = {}
        for desc in catalog.values():
            key = f"{desc.topic}::{desc.action}"
            name_to_desc[key] = desc
            options.append(key)

        options = options[:250] + ["none_of_the_above"]

        choice_res = await self.client.decide_choice(
            state={"query": query},
            instructions="Select the tool most likely to fulfill the user's intent.",
            options=options,
        )

        selected = []
        if choice_res.choice in name_to_desc:
            selected.append(name_to_desc[choice_res.choice])

        # If probability distribution is available, grab top candidates
        if choice_res.probabilities:
            sorted_opts = sorted(
                choice_res.probabilities.items(), key=lambda item: item[1], reverse=True
            )
            for opt_key, prob in sorted_opts:
                if opt_key in name_to_desc and name_to_desc[opt_key] not in selected:
                    selected.append(name_to_desc[opt_key])
                if len(selected) >= self.max_candidates:
                    break

        return selected if selected else list(catalog.values())[:self.max_candidates]
