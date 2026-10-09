from __future__ import annotations

from typing import Any, Dict, List, Literal, Optional, Union
from pydantic import BaseModel, Field


class Noul(BaseModel):
    """Binary (Yes/No) probability question."""
    type: Literal["noul"] = "noul"
    instructions: str = Field(..., description="Binary question or statement to evaluate against state.")


class Choice(BaseModel):
    """Categorical selection question (up to 255 options)."""
    type: Literal["choice"] = "choice"
    instructions: str = Field(..., description="Instructions for selecting the most appropriate option.")
    options: List[str] = Field(..., min_length=1, max_length=255, description="Candidate choices.")


class Score(BaseModel):
    """Ordinal rating question over 2 to 10 ordered levels."""
    type: Literal["score"] = "score"
    instructions: str = Field(..., description="Instructions for rating state against ordered rubric.")
    levels: List[str] = Field(..., min_length=2, max_length=10, description="Ordered levels, e.g. low/med/high.")


QuestionType = Union[Noul, Choice, Score]


class NoulAnswer(BaseModel):
    """Result for a Noul question: probability between 0.0 and 1.0."""
    type: Literal["noul"] = "noul"
    noul: float = Field(..., ge=0.0, le=1.0, description="Probability that the statement is true.")


class ChoiceAnswer(BaseModel):
    """Result for a Choice question: chosen option, probabilities, and confidence."""
    type: Literal["choice"] = "choice"
    choice: str = Field(..., description="Selected option.")
    probabilities: Dict[str, float] = Field(default_factory=dict, description="Probability distribution across all options.")
    confidence: float = Field(1.0, ge=0.0, le=1.0, description="Calibrated confidence score.")


class ScoreAnswer(BaseModel):
    """Result for a Score question: weighted score, level probabilities, and confidence."""
    type: Literal["score"] = "score"
    score: float = Field(..., description="Probability-weighted score along ordered levels.")
    probabilities: Dict[str, float] = Field(default_factory=dict, description="Probability for each level.")
    confidence: float = Field(1.0, ge=0.0, le=1.0, description="Calibrated confidence score.")


AnswerType = Union[NoulAnswer, ChoiceAnswer, ScoreAnswer]


class DecisionResponse(BaseModel):
    """Response containing answers for all submitted questions."""
    answers: Dict[str, AnswerType] = Field(default_factory=dict)
    latency_ms: Optional[float] = None
    cached: bool = False
