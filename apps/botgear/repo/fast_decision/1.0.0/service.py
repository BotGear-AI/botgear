# unmanaged
"""FastDecisionService — in-process System One decision service.

Provides machine-native, sub-150ms evaluations using TypeSafe AI (Jev)
or deterministic offline heuristics for robotics reflex loops, tool routing,
and instant physical actuation.
"""
from __future__ import annotations

import asyncio
import logging
from typing import Any, Dict, List, Literal, Optional

from pydantic import Field, SecretStr
from rlx_bus import ServiceConfig

from botgear.framework import Service, service_method
from rlx_decision import (
    Choice,
    DecisionClient,
    Noul,
    Score,
    create_decision_client,
)

logger = logging.getLogger(__name__)


class FastDecisionConfig(ServiceConfig):
    api_key: Optional[SecretStr] = Field(
        None, description="TypeSafe AI API key (from console.typesafe.ai). Encrypted on disk."
    )
    base_url: str = Field("https://api.typesafe.ai", description="API base URL.")
    confidence_threshold: float = Field(
        0.95, ge=0.5, le=1.0, description="Minimum confidence score for Zone 3 autonomous execution."
    )
    residency: Literal["local_only", "cloud_ok"] = Field(
        "local_only", description="local_only forces offline evaluation; cloud_ok permits API calls."
    )
    timeout_seconds: float = Field(2.0, ge=0.1, le=10.0, description="Max time to wait for decision.")


class FastDecisionService(Service):
    config_class = FastDecisionConfig

    def __init__(self, meta: Any, config: Dict[str, Any]) -> None:
        super().__init__(meta, config)
        self._halted = False
        raw_key = self.config.api_key.get_secret_value() if self.config.api_key else None
        is_mock = self.config.residency == "local_only" or not raw_key or raw_key.lower().startswith("mock")
        self.client = create_decision_client(
            api_key=raw_key if not is_mock else None,
            mock=is_mock,
        )

    async def on_start(self) -> None:
        self._halted = False
        raw_key = self.config.api_key.get_secret_value() if self.config.api_key else None
        is_mock = self.config.residency == "local_only" or not raw_key or raw_key.lower().startswith("mock")

        self.client = create_decision_client(
            api_key=raw_key if not is_mock else None,
            mock=is_mock,
        )
        self.publish(
            "state",
            {
                "status": "ready",
                "residency": self.config.residency,
                "confidence_threshold": self.config.confidence_threshold,
                "mode": "offline_mock" if is_mock else "cloud_jev",
            },
            retained=True,
        )
        self._control_task = asyncio.create_task(self.run_control_loop())

    async def on_stop(self) -> None:
        task = getattr(self, "_control_task", None)
        if task is not None:
            task.cancel()

    async def on_halt(self, event: Dict[str, Any]) -> None:
        self._halted = True
        logger.warning("fast_decision %s: runtime HALT acknowledged", self.proxy_id)

    async def on_resume(self, event: Dict[str, Any]) -> None:
        self._halted = False
        logger.info("fast_decision %s: runtime RESUME acknowledged", self.proxy_id)

    # ── @service_method actions ───────────────────────────────────────

    @service_method("evaluate")
    async def evaluate(self, state: Any, questions: Dict[str, Dict[str, Any]]) -> Dict[str, Any]:
        """Evaluate state against an arbitrary dictionary of questions."""
        if self._halted:
            return {"error": "Runtime is HALTED", "halted": True}

        parsed_q = {}
        for q_id, q_data in questions.items():
            q_type = q_data.get("type", "noul")
            inst = q_data.get("instructions", "")
            if q_type == "noul":
                parsed_q[q_id] = Noul(instructions=inst)
            elif q_type == "choice":
                parsed_q[q_id] = Choice(instructions=inst, options=q_data.get("options", []))
            elif q_type == "score":
                parsed_q[q_id] = Score(instructions=inst, levels=q_data.get("levels", ["low", "high"]))

        resp = await self.client.system_one(state, parsed_q)
        return resp.model_dump()

    @service_method("decide_noul")
    async def decide_noul(self, state: Any, instructions: str) -> float:
        """Evaluate a single binary statement; returns probability 0.0 to 1.0."""
        if self._halted:
            return 0.0
        return await self.client.decide_noul(state, instructions)

    @service_method("decide_choice")
    async def decide_choice(self, state: Any, instructions: str, options: List[str]) -> Dict[str, Any]:
        """Categorical selection among options."""
        if self._halted:
            return {"choice": "none", "confidence": 0.0, "halted": True}
        res = await self.client.decide_choice(state, instructions, options)
        return res.model_dump()

    @service_method("decide_score")
    async def decide_score(self, state: Any, instructions: str, levels: List[str]) -> Dict[str, Any]:
        """Ordinal rating along ordered levels."""
        if self._halted:
            return {"score": 1.0, "confidence": 0.0, "halted": True}
        res = await self.client.decide_score(state, instructions, levels)
        return res.model_dump()

    @service_method("evaluate_lighting")
    async def evaluate_lighting(
        self, utterance: str, registered_lights: Optional[List[str]] = None
    ) -> Dict[str, Any]:
        """Specialized fast reflex evaluator for voice lighting commands on worke."""
        if self._halted:
            return {"is_direct": False, "halted": True}

        lights = registered_lights or ["office_light", "workbench", "desk_lamp"]
        questions = {
            "is_direct": Noul(
                instructions="Is this an unambiguous direct command to change the power state of an active light or switch?"
            ),
            "target_device": Choice(
                instructions="Select the target light or switch mentioned in the command.",
                options=lights + ["none"],
            ),
            "target_state": Choice(
                instructions="What is the requested target state?",
                options=["on", "off", "toggle", "none"],
            ),
            "spoken_ack": Choice(
                instructions="Select a concise confirmation phrase.",
                options=["turning off the light", "turning on the light", "toggling the light", "done"],
            ),
        }

        resp = await self.client.system_one(utterance, questions)
        answers = resp.answers

        is_direct_noul = getattr(answers.get("is_direct"), "noul", 0.0)
        target_device = getattr(answers.get("target_device"), "choice", "none")
        target_state = getattr(answers.get("target_state"), "choice", "none")
        spoken_ack = getattr(answers.get("spoken_ack"), "choice", "done")
        confidence = min(
            getattr(answers.get("target_device"), "confidence", 0.0),
            getattr(answers.get("target_state"), "confidence", 0.0),
        )

        meets_threshold = (
            is_direct_noul >= 0.90
            and confidence >= self.config.confidence_threshold
            and target_device != "none"
            and target_state in ("on", "off", "toggle")
        )

        return {
            "is_direct": meets_threshold,
            "target_device": target_device,
            "target_state": target_state,
            "spoken_ack": spoken_ack,
            "confidence": confidence,
            "latency_ms": resp.latency_ms,
        }

    @service_method("evaluate_pan_tilt")
    async def evaluate_pan_tilt(self, utterance: str) -> Dict[str, Any]:
        """Specialized fast reflex evaluator for pan-tilt head gaze commands on worke."""
        if self._halted:
            return {"is_gaze_command": False, "halted": True}

        questions = {
            "is_gaze_command": Noul(
                instructions="Does this utterance command a physical reorientation of the pan-tilt head/camera?"
            ),
            "target_pose": Choice(
                instructions="Select the target gaze preset orientation.",
                options=["look_up", "look_down", "look_ahead", "look_left", "look_right", "none"],
            ),
            "spoken_ack": Choice(
                instructions="Select a concise acknowledgment phrase.",
                options=["looking up", "looking down", "centering camera", "looking left", "looking right", "on it"],
            ),
            "requires_llm_followup": Noul(
                instructions="Does the utterance also contain a secondary conversational query requiring visual scene analysis or general reasoning?"
            ),
        }

        resp = await self.client.system_one(utterance, questions)
        answers = resp.answers

        is_gaze_noul = getattr(answers.get("is_gaze_command"), "noul", 0.0)
        target_pose = getattr(answers.get("target_pose"), "choice", "none")
        spoken_ack = getattr(answers.get("spoken_ack"), "choice", "on it")
        requires_followup = getattr(answers.get("requires_llm_followup"), "noul", 0.0) >= 0.70
        confidence = getattr(answers.get("target_pose"), "confidence", 0.0)

        meets_threshold = (
            is_gaze_noul >= 0.90
            and confidence >= self.config.confidence_threshold
            and target_pose != "none"
        )

        return {
            "is_gaze_command": meets_threshold,
            "target_pose": target_pose,
            "spoken_ack": spoken_ack,
            "requires_llm_followup": requires_followup,
            "confidence": confidence,
            "latency_ms": resp.latency_ms,
        }
