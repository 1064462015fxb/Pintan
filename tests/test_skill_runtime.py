"""验证 Deep Agents 的公开 messages 输出，不依赖未返回的内部 metadata。"""
import unittest
from types import SimpleNamespace

from pintan.agents import _skill_trace, _text, _validate_assessments


class SkillTraceTests(unittest.TestCase):
    def test_validation_reports_all_bad_records(self):
        with self.assertRaises(ValueError) as result:
            _validate_assessments([{"search_id": "bad0"}, {"search_id": "bad1"}], {}, [])
        self.assertIn("bad0", str(result.exception))
        self.assertIn("bad1", str(result.exception))

    def test_ai_content_blocks_not_tool_output(self):
        response = {"messages": [SimpleNamespace(type="tool", content="wrong tool output"),
            SimpleNamespace(type="ai", content=[{"type": "text", "text": '{"assessments": []}'}])]}
        self.assertEqual(_text(response), '{"assessments": []}')

    def test_tool_output_cannot_be_final_answer(self):
        with self.assertRaises(ValueError):
            _text({"messages": [SimpleNamespace(type="tool", content="not an answer")]})

    def test_successful_skill_read(self):
        response = {"messages": [
            SimpleNamespace(tool_calls=[{"id": "t1", "name": "read_file", "args": {"file_path": "/skills/stable-selection/SKILL.md"}}]),
            SimpleNamespace(tool_call_id="t1", status="success", content="name: stable-selection"),
        ]}
        self.assertTrue(_skill_trace(response, "stable-selection")["skill_read"])

    def test_failed_read_is_not_success(self):
        response = {"messages": [
            SimpleNamespace(tool_calls=[{"id": "t1", "name": "read_file", "args": {"file_path": "/skills/stable-selection/SKILL.md"}}]),
            SimpleNamespace(tool_call_id="t1", status="error", content="stable-selection not found"),
        ]}
        with self.assertRaises(ValueError):
            _skill_trace(response, "stable-selection")

    def test_other_skill_cannot_satisfy_trace(self):
        with self.assertRaises(ValueError):
            _skill_trace({"messages": []}, "trending-selection")
