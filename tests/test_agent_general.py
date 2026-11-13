import asyncio
import unittest
from unittest.mock import patch

from backend import agent
from backend.llm import LLMResponse


class FakeChain:
    def __init__(self, text: str):
        self.text = text
        self.seen_tools = None

    async def stream(self, system, messages, tools):
        self.seen_tools = tools
        yield self.text
        yield LLMResponse(text=self.text, tool_calls=[], provider="fake", model="m", elapsed_ms=1)


def _collect(session_id: str, message: str):
    async def run():
        return [e async for e in agent.stream_answer(session_id, message, [])]

    return asyncio.run(run())


class GeneralQuestionWithoutTrackerTests(unittest.TestCase):
    def test_general_question_answers_without_tracker(self):
        chain = FakeChain("Paris is the capital of France.")
        with patch.object(agent, "get_active_tracker", return_value=None), patch.object(
            agent, "get_llm_chain", return_value=chain
        ):
            events = _collect("s1", "What is the capital of France?")

        done = events[-1]
        self.assertEqual(done["type"], "done")
        self.assertEqual(done["answer"], "Paris is the capital of France.")
        self.assertEqual(done["provider"], "general")
        self.assertEqual(chain.seen_tools, [])
        self.assertTrue(any(e["type"] == "delta" for e in events))


if __name__ == "__main__":
    unittest.main()
