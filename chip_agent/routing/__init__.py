"""Model routing: the single locus of model choice."""

from chip_agent.routing.gateway import (
    CompletionBackend,
    CompletionResult,
    GatewayError,
    LiteLLMBackend,
    LiteLLMGateway,
)
from chip_agent.routing.policy import (
    FailureType,
    ResolutionError,
    loop_for,
    params_for,
    resolve_model,
)
from chip_agent.routing.router import LiteLLMRouter, Scorer

__all__ = [
    "CompletionBackend",
    "CompletionResult",
    "FailureType",
    "GatewayError",
    "LiteLLMBackend",
    "LiteLLMGateway",
    "LiteLLMRouter",
    "ResolutionError",
    "Scorer",
    "loop_for",
    "params_for",
    "resolve_model",
]
