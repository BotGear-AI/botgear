from __future__ import annotations

import re
import time
from typing import Any, Callable, Dict, List, Optional, Union

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


class MockDecisionClient:
    """Deterministic mock provider for System One decisions.

    Provides rule-based evaluations for common robotics testing scenarios
    (lighting, pan-tilt head control, emergency halt, exit intent) and allows
    registering custom handlers for unit/integration testing.
    """

    def __init__(self, latency_sim_ms: float = 5.0):
        self.latency_sim_ms = latency_sim_ms
        self._custom_handlers: Dict[str, Callable[[Any, QuestionType], Any]] = {}

    def register_handler(self, question_id: str, handler: Callable[[Any, QuestionType], Any]) -> None:
        self._custom_handlers[question_id] = handler

    def evaluate_sync(self, state: Any, questions: Dict[str, QuestionType]) -> DecisionResponse:
        t0 = time.perf_counter()
        answers = {}

        for q_id, q in questions.items():
            if q_id in self._custom_handlers:
                answers[q_id] = self._custom_handlers[q_id](state, q)
                continue

            if isinstance(q, Noul):
                answers[q_id] = self._eval_noul(state, q)
            elif isinstance(q, Choice):
                answers[q_id] = self._eval_choice(state, q)
            elif isinstance(q, Score):
                answers[q_id] = self._eval_score(state, q)

        elapsed_ms = (time.perf_counter() - t0) * 1000.0 + self.latency_sim_ms
        return DecisionResponse(answers=answers, latency_ms=round(elapsed_ms, 2))

    async def system_one(self, state: Any, questions: Dict[str, QuestionType]) -> DecisionResponse:
        return self.evaluate_sync(state, questions)

    # ── rule heuristics ────────────────────────────────────────────────

    def _extract_text(self, state: Any) -> str:
        if isinstance(state, str):
            return state.lower()
        if isinstance(state, dict):
            parts = []
            for k in ("utterance", "text", "query", "message", "latest_utterance"):
                v = state.get(k)
                if isinstance(v, str):
                    parts.append(v.lower())
            dialogue = state.get("dialogue")
            if isinstance(dialogue, dict):
                for k in ("latest_utterance", "text", "utterance"):
                    v = dialogue.get(k)
                    if isinstance(v, str):
                        parts.append(v.lower())
            if parts:
                return " ".join(parts)
            return str(state).lower()
        return str(state).lower()

    def _eval_noul(self, state: Any, q: Noul) -> NoulAnswer:
        text = self._extract_text(state)
        inst = q.instructions.lower()

        # Lighting / Power actuation
        if any(w in inst for w in ("actuation", "power state", "light", "switch", "lamp", "turn on", "turn off")):
            is_light_cmd = bool(re.search(r"\b(turn|switch|toggle)\b.*\b(light|lamp|cord|desk|office|workbench|switch|plug|relay|outlet)\b", text)
                               or re.search(r"\b(light|lamp|cord|switch|plug|relay|outlet)\b.*\b(on|off)\b", text))
            return NoulAnswer(noul=0.99 if is_light_cmd else 0.05)

        # Pan-Tilt head motion
        if any(w in inst for w in ("pan-tilt", "head", "camera", "look", "tilt", "reorientation")):
            is_pan_tilt = bool(re.search(r"\b(look|tilt|pan|aim|point)\b.*\b(up|down|left|right|ahead|forward|floor|shelf|ceiling)\b", text)
                              or re.search(r"\b(center|level)\b.*\b(head|camera)\b", text))
            return NoulAnswer(noul=0.98 if is_pan_tilt else 0.02)

        # Emergency stop / Halt
        if any(w in inst for w in ("emergency", "halt", "stop", "motor cut")):
            is_halt = bool(re.search(r"\b(emergency|estop|freeze|halt|abort|stop moving|stop now)\b", text))
            return NoulAnswer(noul=0.99 if is_halt else 0.01)

        # Exit intent
        if any(w in inst for w in ("exit", "end conversation", "goodbye", "dismiss")):
            is_exit = bool(re.search(r"\b(goodbye|bye|see ya|dismiss|stop talking|exit|quit)\b", text))
            return NoulAnswer(noul=0.99 if is_exit else 0.05)

        # Secondary question / LLM followup needed
        if any(w in inst for w in ("secondary", "visual", "followup", "reasoning", "tell me", "what you see")):
            has_followup = bool(re.search(r"\b(and\b.*\b(tell|inspect|check|find|see|what|describe))\b", text))
            return NoulAnswer(noul=0.96 if has_followup else 0.10)

        # Fallback keyword match
        match = any(word in text for word in inst.split() if len(word) > 3)
        return NoulAnswer(noul=0.85 if match else 0.15)

    def _eval_choice(self, state: Any, q: Choice) -> ChoiceAnswer:
        text = self._extract_text(state)
        inst = q.instructions.lower()
        options = q.options

        # Power state: on / off / toggle
        if set(options).issubset({"on", "off", "toggle", "none"}):
            if re.search(r"\b(off|extinguish|kill|disable)\b", text):
                return ChoiceAnswer(choice="off", probabilities={"off": 0.98, "on": 0.01, "toggle": 0.01}, confidence=0.98)
            if re.search(r"\b(on|light up|enable|activate)\b", text):
                return ChoiceAnswer(choice="on", probabilities={"on": 0.98, "off": 0.01, "toggle": 0.01}, confidence=0.98)
            if re.search(r"\b(toggle|flip|switch)\b", text):
                return ChoiceAnswer(choice="toggle", probabilities={"toggle": 0.95, "on": 0.03, "off": 0.02}, confidence=0.95)

        # Pan-Tilt presets
        if any("look_" in opt or opt in ("up", "down", "left", "right", "ahead", "center") for opt in options):
            if re.search(r"\b(up|ceiling|shelf|sky)\b", text):
                pick = "look_up" if "look_up" in options else "up"
                return ChoiceAnswer(choice=pick, probabilities={pick: 0.98}, confidence=0.98)
            if re.search(r"\b(down|floor|ground)\b", text):
                pick = "look_down" if "look_down" in options else "down"
                return ChoiceAnswer(choice=pick, probabilities={pick: 0.98}, confidence=0.98)
            if re.search(r"\b(left)\b", text):
                pick = "look_left" if "look_left" in options else "left"
                return ChoiceAnswer(choice=pick, probabilities={pick: 0.98}, confidence=0.98)
            if re.search(r"\b(right)\b", text):
                pick = "look_right" if "look_right" in options else "right"
                return ChoiceAnswer(choice=pick, probabilities={pick: 0.98}, confidence=0.98)
            if re.search(r"\b(center|forward|ahead|straight)\b", text):
                pick = "look_ahead" if "look_ahead" in options else ("center" if "center" in options else options[0])
                return ChoiceAnswer(choice=pick, probabilities={pick: 0.98}, confidence=0.98)

        # Spoken confirmation phrases
        if any("turning" in opt or "light" in opt or "looking" in opt for opt in options):
            if re.search(r"\b(look|tilt|camera|head)\b", text):
                match = next((o for o in options if "look" in o), options[0])
                return ChoiceAnswer(choice=match, probabilities={match: 0.97}, confidence=0.97)
            if re.search(r"\b(off)\b", text):
                match = next((o for o in options if "off" in o), options[0])
                return ChoiceAnswer(choice=match, probabilities={match: 0.96}, confidence=0.96)
            if re.search(r"\b(on)\b", text):
                match = next((o for o in options if "on" in o), options[0])
                return ChoiceAnswer(choice=match, probabilities={match: 0.96}, confidence=0.96)

        # Device / tool matching
        best_opt = None
        best_score = -1
        for opt in options:
            if opt in ("none", "none_of_the_above"):
                continue
            words = [w for w in re.split(r"[-_/: ]+", opt.lower()) if len(w) > 2]
            score = sum(1 for w in words if w in text)
            if score > best_score:
                best_score = score
                best_opt = opt

        if best_opt and best_score > 0:
            probs = {o: (0.95 if o == best_opt else 0.05 / max(1, len(options) - 1)) for o in options}
            return ChoiceAnswer(choice=best_opt, probabilities=probs, confidence=0.95)

        # Default fallback
        fallback = "none" if "none" in options else ("none_of_the_above" if "none_of_the_above" in options else options[0])
        return ChoiceAnswer(choice=fallback, probabilities={fallback: 0.60}, confidence=0.60)

    def _eval_score(self, state: Any, q: Score) -> ScoreAnswer:
        text = self._extract_text(state)
        levels = q.levels
        n = len(levels)

        # Urgency / risk rubric
        if re.search(r"\b(critical|emergency|danger|crash|hit)\b", text):
            return ScoreAnswer(score=float(n), probabilities={levels[-1]: 0.95}, confidence=0.95)
        if re.search(r"\b(warning|caution|watch out|close)\b", text):
            idx = max(0, n // 2)
            return ScoreAnswer(score=float(idx + 1), probabilities={levels[idx]: 0.90}, confidence=0.90)

        # Default low / safe
        return ScoreAnswer(score=1.0, probabilities={levels[0]: 0.92}, confidence=0.92)
