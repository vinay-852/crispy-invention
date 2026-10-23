import json
import logging
import os
import uuid

log = logging.getLogger(__name__)

APP_NAME = "stockwatch-ai"
USER_ID = "stockwatch-user"


class AdkAgent:
    """Shared Google ADK agent: Gemini model, no tools, JSON output contract.

    Tool-less on purpose — data comes from reliable free endpoints (yfinance,
    Google News RSS) fed into the prompt, not from web-search tools. Every
    method returns None when no Google API key is set, so callers fall back to
    deterministic logic and the money loop keeps working offline.

    (ponytail: requires GOOGLE_API_KEY / GEMINI_API_KEY and a Gemini model;
    auth is read from the environment by google-genai.)
    """

    def __init__(self, name: str, instruction: str, model: str = "gemini-2.0-flash"):
        self.name = name
        self.instruction = instruction
        self.model = model
        self._runner = None
        self._session_service = None

    @property
    def available(self) -> bool:
        return bool(os.environ.get("GOOGLE_API_KEY") or os.environ.get("GEMINI_API_KEY"))

    def _ensure_runner(self) -> None:
        if self._runner is not None:
            return
        from google.adk.agents import Agent
        from google.adk.runners import Runner
        from google.adk.sessions import InMemorySessionService

        agent = Agent(name=self.name, model=self.model, instruction=self.instruction)
        self._session_service = InMemorySessionService()
        self._runner = Runner(agent=agent, app_name=APP_NAME, session_service=self._session_service)

    async def complete_json(self, prompt: str) -> dict | None:
        """One-shot structured completion; None on unavailable/failure/non-JSON."""
        if not self.available:
            return None
        self._ensure_runner()
        session_id = uuid.uuid4().hex
        await self._session_service.create_session(
            app_name=APP_NAME, user_id=USER_ID, session_id=session_id)
        try:
            from google.genai import types
            content = types.Content(role="user", parts=[types.Part(text=prompt)])
            async for event in self._runner.run_async(
                    user_id=USER_ID, session_id=session_id, new_message=content):
                if event.is_final_response():
                    text = event.content.parts[0].text if event.content and event.content.parts else ""
                    return self._parse_json(text)
        except Exception:
            log.exception("%s call failed", self.name)
        return None

    @staticmethod
    def _parse_json(text: str) -> dict | None:
        text = text.strip().strip("`")
        if text.lower().startswith("json"):
            text = text[4:]
        start, end = text.find("{"), text.rfind("}")
        if start == -1 or end == -1:
            return None
        try:
            return json.loads(text[start:end + 1])
        except json.JSONDecodeError:
            log.warning("%s returned non-JSON: %.200s", "agent", text)
            return None