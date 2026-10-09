from __future__ import annotations

import asyncio
import json
import logging
import os
from typing import Any, Dict, List, Optional, Union

import requests
from rlx_decision.mock import MockDecisionClient
from rlx_decision.types import (
    Choice,
    ChoiceAnswer,
    DecisionResponse,
    Noul,
    NoulAnswer,
    QuestionType,
    Score,
    ScoreAnswer,
)

logger = logging.getLogger(__name__)

DEFAULT_BASE_URL = "https://api.typesafe.ai"


class DecisionClient:
    """Client for TypeSafe AI / Jev System One decision models.

    Supports both live cloud API calls to TypeSafe AI and offline/testing
    mock evaluation via MockDecisionClient.
    """

    def __init__(
        self,
        api_key: Optional[str] = None,
        base_url: Optional[str] = None,
        timeout_seconds: float = 3.0,
        mock: Optional[bool] = None,
    ):
        # Live calls send state off-host, so they need an explicit opt-in:
        # an ``api_key`` argument, or ``mock=False``. TYPESAFE_API_KEY in
        # the environment is only read after that opt-in — on its own it
        # never turns a default-constructed client live.
        if mock is None:
            mock = not api_key or api_key.lower().startswith("mock")
        self.mock = mock
        self.api_key = api_key or (None if mock else os.getenv("TYPESAFE_API_KEY"))
        self.base_url = (base_url or os.getenv("TYPESAFE_BASE_URL") or DEFAULT_BASE_URL).rstrip("/")
        self.timeout_seconds = timeout_seconds

        self._mock_client = MockDecisionClient() if self.mock else None

    async def system_one(self, state: Any, questions: Dict[str, QuestionType]) -> DecisionResponse:
        """Evaluate state against a dict of questions in parallel."""
        if self.mock:
            return await self._mock_client.system_one(state, questions)

        return await asyncio.to_thread(self._call_remote, state, questions)

    async def decide_noul(self, state: Any, instructions: str) -> float:
        """Shortcut for single binary question."""
        res = await self.system_one(state, {"q": Noul(instructions=instructions)})
        ans = res.answers.get("q")
        if isinstance(ans, NoulAnswer):
            return ans.noul
        return 0.5

    async def decide_choice(self, state: Any, instructions: str, options: List[str]) -> ChoiceAnswer:
        """Shortcut for single choice question."""
        res = await self.system_one(state, {"q": Choice(instructions=instructions, options=options)})
        ans = res.answers.get("q")
        if isinstance(ans, ChoiceAnswer):
            return ans
        return ChoiceAnswer(choice=options[0] if options else "none", confidence=0.0)

    async def decide_score(self, state: Any, instructions: str, levels: List[str]) -> ScoreAnswer:
        """Shortcut for single score question."""
        res = await self.system_one(state, {"q": Score(instructions=instructions, levels=levels)})
        ans = res.answers.get("q")
        if isinstance(ans, ScoreAnswer):
            return ans
        return ScoreAnswer(score=1.0, confidence=0.0)

    def _call_remote(self, state: Any, questions: Dict[str, QuestionType]) -> DecisionResponse:
        url = f"{self.base_url}/v1/system-one"
        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
        }
        payload = {
            "state": state,
            "questions": {k: q.model_dump() for k, q in questions.items()},
        }

        try:
            resp = requests.post(url, json=payload, headers=headers, timeout=self.timeout_seconds)
            if resp.status_code == 200:
                data = resp.json()
                raw_answers = data.get("answers") or {}
                parsed_answers = {}
                for k, ans in raw_answers.items():
                    q_type = questions[k].type if k in questions else ans.get("type")
                    if q_type == "noul" or "noul" in ans:
                        parsed_answers[k] = NoulAnswer(noul=float(ans.get("noul", 0.5)))
                    elif q_type == "choice" or "choice" in ans:
                        parsed_answers[k] = ChoiceAnswer(
                            choice=str(ans.get("choice")),
                            probabilities=ans.get("probabilities") or {},
                            confidence=float(ans.get("confidence", 1.0)),
                        )
                    elif q_type == "score" or "score" in ans:
                        parsed_answers[k] = ScoreAnswer(
                            score=float(ans.get("score", 1.0)),
                            probabilities=ans.get("probabilities") or {},
                            confidence=float(ans.get("confidence", 1.0)),
                        )
                return DecisionResponse(answers=parsed_answers)
            else:
                logger.warning("Remote decision API error HTTP %s: %s", resp.status_code, resp.text)
                # Fallback to mock on remote error
                if not self._mock_client:
                    self._mock_client = MockDecisionClient()
                return self._mock_client.evaluate_sync(state, questions)
        except Exception as exc:
            logger.warning("Exception calling remote decision API (%s), falling back to mock", exc)
            if not self._mock_client:
                self._mock_client = MockDecisionClient()
            return self._mock_client.evaluate_sync(state, questions)


def create_decision_client(api_key: Optional[str] = None, mock: Optional[bool] = None) -> DecisionClient:
    return DecisionClient(api_key=api_key, mock=mock)
