import time
import asyncio
import logging
from typing import Any, Dict

from reachy_mini_conversation_app.tools.core_tools import Tool, ToolDependencies


logger = logging.getLogger(__name__)


class AskOpenClaw(Tool):
    """Send a task or question to OpenClaw, your AI partner on Discord."""

    name = "ask_openclaw"
    description = (
        "Send a task or question to OpenClaw, your AI partner on Discord. "
        "Use this when you need information lookup, web research, or something "
        "that requires text-based tools. OpenClaw will process the task and "
        "send its findings back to you."
    )
    parameters_schema = {
        "type": "object",
        "properties": {
            "task": {
                "type": "string",
                "description": "The task or question for OpenClaw to handle.",
            },
        },
        "required": ["task"],
    }

    async def __call__(self, deps: ToolDependencies, **kwargs: Any) -> Dict[str, Any]:
        task = kwargs.get("task", "").strip()
        if not task:
            return {"error": "task is required"}

        if deps.bridge_state is None:
            return {"error": "bridge not available"}

        logger.info("Tool call: ask_openclaw task=%s", task[:100])

        try:
            loop = asyncio.get_running_loop()
            await deps.bridge_state.broadcast({
                "type": "task",
                "task": task,
                "timestamp": int(time.time() * 1000),
            })
            return {"status": "sent", "task": task}
        except Exception as e:
            logger.error("Failed to send task to OpenClaw: %s", e)
            return {"error": f"Failed to send: {e}"}
