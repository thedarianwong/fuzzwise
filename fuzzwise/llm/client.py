"""
Ollama API client wrapper for local LLM inference.
"""

from __future__ import annotations

import json
import logging
import time
from typing import Any, Optional

import requests
from requests.exceptions import RequestException

logger = logging.getLogger(__name__)


class OllamaClient:
    """
    Client for interacting with a local Ollama server.

    Handles:
        - Health checks and model availability
        - Prompt generation with configurable parameters
        - Retry logic with exponential backoff
        - Response parsing and validation
    """

    # Ollama API endpoints
    GENERATE_URL = "/api/generate"
    TAGS_URL = "/api/tags"
    PULL_URL = "/api/pull"

    def __init__(
        self,
        host: str = "http://localhost:11434",
        model: str = "qwen2.5:7b",
        temperature: float = 0.8,
        top_p: float = 0.95,
        timeout_seconds: float = 30.0,
        max_retries: int = 2,
    ):
        """
        Initialize the Ollama client.

        Args:
            host: Ollama server URL
            model: Model name to use
            temperature: Sampling temperature (0.0-1.0)
            top_p: Nucleus sampling parameter
            timeout_seconds: Request timeout
            max_retries: Number of retry attempts on failure
        """
        self.host = host.rstrip('/')
        self.model = model
        self.temperature = temperature
        self.top_p = top_p
        self.timeout = timeout_seconds
        self.max_retries = max_retries

        self._available = None
        self._check_availability()

    def _check_availability(self) -> bool:
        """Check if Ollama is running and the model is available."""
        if self._available is not None:
            return self._available

        try:
            # Check if Ollama is running
            resp = requests.get(f"{self.host}{self.TAGS_URL}", timeout=5)
            resp.raise_for_status()

            data = resp.json()
            models = data.get("models", [])
            model_names = [m.get("name", "") for m in models]

            if self.model not in model_names:
                logger.warning(
                    f"Model '{self.model}' not found in Ollama. "
                    f"Available: {model_names[:5]}"
                )
                self._available = False
            else:
                logger.info(f"Ollama client initialized with model: {self.model}")
                self._available = True

        except RequestException as e:
            logger.warning(f"Cannot connect to Ollama at {self.host}: {e}")
            self._available = False

        return self._available

    @property
    def is_available(self) -> bool:
        """Return True if Ollama is available and model is loaded."""
        return self._check_availability()

    def generate(self, prompt: str, max_tokens: int = 150) -> str:
        """
        Generate a completion for the given prompt.

        Args:
            prompt: The prompt to send to the LLM
            max_tokens: Maximum number of tokens to generate

        Returns:
            The generated text, stripped of whitespace

        Raises:
            RequestException: If all retries fail
        """
        if not self.is_available:
            raise RuntimeError("Ollama not available")

        url = f"{self.host}{self.GENERATE_URL}"

        payload = {
            "model": self.model,
            "prompt": prompt,
            "stream": False,
            "options": {
                "temperature": self.temperature,
                "top_p": self.top_p,
                "num_predict": max_tokens,
                "stop": ["Value:", "```"],  # Stop at natural boundaries
            }
        }

        last_error = None
        for attempt in range(self.max_retries):
            try:
                start_time = time.time()
                response = requests.post(url, json=payload, timeout=self.timeout)
                response.raise_for_status()

                elapsed = time.time() - start_time
                data = response.json()
                result = data.get("response", "").strip()

                logger.debug(
                    f"Ollama generation took {elapsed:.2f}s, "
                    f"prompt length: {len(prompt)}, "
                    f"response length: {len(result)}"
                )
                return result

            except RequestException as e:
                last_error = e
                logger.debug(f"Ollama attempt {attempt + 1} failed: {e}")
                if attempt < self.max_retries - 1:
                    # Exponential backoff: 0.5s, 1s, 2s, etc.
                    wait_time = 0.5 * (2 ** attempt)
                    time.sleep(wait_time)

        raise last_error or RuntimeError("All Ollama attempts failed")

    def pull_model(self, model: Optional[str] = None) -> bool:
        """
        Attempt to pull the model if not available.

        Returns:
            True if model is available after pull attempt
        """
        model_to_pull = model or self.model

        try:
            url = f"{self.host}{self.PULL_URL}"
            payload = {"name": model_to_pull, "stream": False}

            logger.info(f"Pulling model {model_to_pull}... (this may take a while)")
            response = requests.post(url, json=payload, timeout=300.0)
            response.raise_for_status()

            # Re-check availability
            self._available = None
            return self.is_available

        except RequestException as e:
            logger.error(f"Failed to pull model {model_to_pull}: {e}")
            return False