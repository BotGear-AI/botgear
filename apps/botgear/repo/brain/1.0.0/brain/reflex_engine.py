# unmanaged
"""FastReflexEngine — System One reflex bypass for brain workflows.

Provides sub-150ms evaluation of user utterances using rlx_decision
(Jev or offline mock heuristics) to execute direct actuation commands
(lighting/power, pan-tilt gaze presets) and concurrent spoken responses,
completely bypassing the multi-second System 2 LLM when confidence >= 0.95.
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Tuple

from botgear.agent_surface.models import ToolDescriptor
from brain.schemas import ProposedAction
from rlx_decision import (
    Choice,
    DecisionClient,
    Noul,
    create_decision_client,
)

logger = logging.getLogger(__name__)


@dataclass
class ReflexResult:
    actions: List[ProposedAction]
    requires_llm_followup: bool = False
    confidence: float = 1.0
    latency_ms: Optional[float] = None
    reflex_kind: str = "direct_actuation"


class FastReflexEngine:
    """Evaluates incoming utterances for direct reflex execution."""

    def __init__(
        self,
        client: Optional[DecisionClient] = None,
        confidence_threshold: float = 0.95,
        enabled: bool = True,
    ):
        self.client = client or create_decision_client()
        self.confidence_threshold = confidence_threshold
        self.enabled = enabled

    async def evaluate_utterance(
        self,
        utterance: str,
        catalog: Dict[str, ToolDescriptor],
    ) -> Optional[ReflexResult]:
        """Check if utterance can be executed as a fast reflex.

        Returns ReflexResult with actions to execute immediately, or None
        if the request should fall through to the System 2 LLM.
        """
        if not self.enabled or not utterance or not utterance.strip():
            return None

        clean_text = utterance.strip()

        # 1. Discover active actuators & speech services from catalog
        light_tools, speech_tool = self._find_lighting_tools(catalog)
        servo_tools = self._find_servo_tools(catalog)

        # 2. Check lighting / power actuation
        if light_tools:
            res = await self._check_lighting_reflex(clean_text, light_tools, speech_tool)
            if res is not None:
                return res

        # 3. Check pan-tilt gaze command
        if servo_tools:
            res = await self._check_pan_tilt_reflex(clean_text, servo_tools, speech_tool)
            if res is not None:
                return res

        return None

    # ── internal discovery ────────────────────────────────────────────

    def _find_lighting_tools(
        self, catalog: Dict[str, ToolDescriptor]
    ) -> Tuple[Dict[str, ToolDescriptor], Optional[ToolDescriptor]]:
        """Find active power_set / power_toggle tools and speech tools."""
        light_tools: Dict[str, ToolDescriptor] = {}
        speech_tool: Optional[ToolDescriptor] = None

        for desc in catalog.values():
            # Match speech tool
            if desc.action == "speak" and speech_tool is None:
                speech_tool = desc

            # Match power tools
            if desc.action in ("power_set", "power_toggle"):
                # Extract device name from topic e.g. /tasmota/office-cord/control -> office-cord
                parts = desc.topic.split("/")
                device_name = parts[2] if len(parts) > 2 else desc.action
                # Prefer power_set: a device exposing both must not have
                # power_set overwritten by power_toggle, or "turn off"
                # would toggle (and turn an already-off light on).
                if desc.action == "power_set" or device_name not in light_tools:
                    light_tools[device_name] = desc

        return light_tools, speech_tool

    def _find_servo_tools(
        self, catalog: Dict[str, ToolDescriptor]
    ) -> Dict[str, ToolDescriptor]:
        """Find active servo_write tools."""
        servo_tools: Dict[str, ToolDescriptor] = {}
        for desc in catalog.values():
            if desc.action == "servo_write":
                parts = desc.topic.split("/")
                axis = parts[2] if len(parts) > 2 else "servo"
                servo_tools[axis] = desc
        return servo_tools

    # ── reflex evaluators ─────────────────────────────────────────────

    async def _check_lighting_reflex(
        self,
        text: str,
        light_tools: Dict[str, ToolDescriptor],
        speech_tool: Optional[ToolDescriptor],
    ) -> Optional[ReflexResult]:
        device_names = list(light_tools.keys())
        questions = {
            "is_direct": Noul(
                instructions="Is this an unambiguous direct command to change the power state of an active light or switch?"
            ),
            "target_device": Choice(
                instructions="Select the target light or switch mentioned in the command.",
                options=device_names + ["none"],
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

        resp = await self.client.system_one(text, questions)
        answers = resp.answers

        is_direct = getattr(answers.get("is_direct"), "noul", 0.0) >= 0.90
        target_device = getattr(answers.get("target_device"), "choice", "none")
        target_state = getattr(answers.get("target_state"), "choice", "none")
        spoken_ack = getattr(answers.get("spoken_ack"), "choice", "done")
        state_conf = getattr(answers.get("target_state"), "confidence", 0.0)
        tool_desc = light_tools.get(target_device)
        if tool_desc is not None:
            confidence = min(getattr(answers.get("target_device"), "confidence", 0.0), state_conf)
        elif len(light_tools) == 1:
            # Only one light exists — "turn the light off" needn't name it.
            tool_desc = next(iter(light_tools.values()))
            confidence = state_conf
        else:
            return None

        if not is_direct or confidence < self.confidence_threshold:
            return None

        if target_state not in ("on", "off", "toggle"):
            return None

        # A toggle-only device can't be driven to an absolute on/off
        # state without knowing its current state — leave it to System 2.
        if tool_desc.action == "power_toggle" and target_state != "toggle":
            return None

        # Build proposed actions
        actions = []
        if target_state == "toggle":
            actions.append(
                ProposedAction(
                    kind="tool",
                    topic=tool_desc.topic,
                    action="power_toggle",
                    args={},
                )
            )
        else:
            actions.append(
                ProposedAction(
                    kind="tool",
                    topic=tool_desc.topic,
                    action="power_set",
                    args={"on": (target_state == "on")},
                )
            )

        # Append non-blocking speech confirmation
        if speech_tool:
            actions.append(
                ProposedAction(
                    kind="tool",
                    topic=speech_tool.topic,
                    action="speak",
                    args={"text": spoken_ack, "interrupt": True},
                )
            )

        return ReflexResult(
            actions=actions,
            requires_llm_followup=False,
            confidence=confidence,
            latency_ms=resp.latency_ms,
            reflex_kind="lighting_reflex",
        )

    async def _check_pan_tilt_reflex(
        self,
        text: str,
        servo_tools: Dict[str, ToolDescriptor],
        speech_tool: Optional[ToolDescriptor],
    ) -> Optional[ReflexResult]:
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

        resp = await self.client.system_one(text, questions)
        answers = resp.answers

        is_gaze = getattr(answers.get("is_gaze_command"), "noul", 0.0) >= 0.90
        target_pose = getattr(answers.get("target_pose"), "choice", "none")
        spoken_ack = getattr(answers.get("spoken_ack"), "choice", "on it")
        requires_followup = getattr(answers.get("requires_llm_followup"), "noul", 0.0) >= 0.70
        confidence = getattr(answers.get("target_pose"), "confidence", 0.0)

        if not is_gaze or confidence < self.confidence_threshold or target_pose == "none":
            return None

        # Map preset to servo axis & angle
        # Worke tilt servo: 90 is level, 120 is up, 60 is down
        # Worke pan servo: 90 is center, 135 is left, 45 is right
        actions = []
        tilt_tool = servo_tools.get("tilt") or servo_tools.get("head") or next((t for k, t in servo_tools.items() if "tilt" in k), None)
        pan_tool = servo_tools.get("pan") or next((t for k, t in servo_tools.items() if "pan" in k), None)

        if target_pose == "look_up" and tilt_tool:
            actions.append(ProposedAction(kind="tool", topic=tilt_tool.topic, action="servo_write", args={"angle": 120}))
        elif target_pose == "look_down" and tilt_tool:
            actions.append(ProposedAction(kind="tool", topic=tilt_tool.topic, action="servo_write", args={"angle": 60}))
        elif target_pose in ("look_ahead", "center"):
            if tilt_tool:
                actions.append(ProposedAction(kind="tool", topic=tilt_tool.topic, action="servo_write", args={"angle": 90}))
            if pan_tool:
                actions.append(ProposedAction(kind="tool", topic=pan_tool.topic, action="servo_write", args={"angle": 90}))
        elif target_pose == "look_left" and pan_tool:
            actions.append(ProposedAction(kind="tool", topic=pan_tool.topic, action="servo_write", args={"angle": 135}))
        elif target_pose == "look_right" and pan_tool:
            actions.append(ProposedAction(kind="tool", topic=pan_tool.topic, action="servo_write", args={"angle": 45}))

        if not actions:
            return None

        # Add spoken confirmation
        if speech_tool:
            actions.append(
                ProposedAction(
                    kind="tool",
                    topic=speech_tool.topic,
                    action="speak",
                    args={"text": spoken_ack, "interrupt": True},
                )
            )

        return ReflexResult(
            actions=actions,
            requires_llm_followup=requires_followup,
            confidence=confidence,
            latency_ms=resp.latency_ms,
            reflex_kind="pan_tilt_reflex",
        )
