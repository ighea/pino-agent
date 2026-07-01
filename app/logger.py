import datetime
import os
import json
from pathlib import Path

# Long values (e.g. a fetched web page or file dump passed as a tool argument/result)
# are truncated before being written to the log so the JSONL file doesn't grow unbounded.
_MAX_LOG_VALUE_CHARS = int(os.getenv("MAX_LOG_VALUE_CHARS", "4000"))


def _truncate_for_log(value, max_chars: int = _MAX_LOG_VALUE_CHARS):
    if isinstance(value, str) and len(value) > max_chars:
        return value[:max_chars] + f"... [truncated, {len(value):,} chars total]"
    if isinstance(value, dict):
        return {k: _truncate_for_log(v, max_chars) for k, v in value.items()}
    if isinstance(value, list):
        return [_truncate_for_log(v, max_chars) for v in value]
    return value


class AgentLogger:
    """
    Manages logging and history persistence for the agent framework.
    Stores all events (inputs, outputs, tool calls) chronologically.
    """
    LOG_FILE = os.getenv("AGENT_LOG_FILE", "data/agent_history.jsonl")

    def __init__(self):
        Path(self.LOG_FILE).parent.mkdir(parents=True, exist_ok=True)
        if not os.path.exists(self.LOG_FILE):
            print(f"Initializing new agent history log file at {self.LOG_FILE}")
        else:
            print(f"Loading existing agent history from {self.LOG_FILE}")

    def _write_log_entry(self, data):
        """Appends a structured log entry to the JSON Lines file."""
        with open(self.LOG_FILE, 'a') as f:
            f.write(json.dumps(data) + '\n')

    def log_event(self, event_type: str, content: dict):
        """
        Logs a general event (e.g., initial message received).
        Event Type examples: 'INPUT', 'TOOL_CALL', 'LLM_DECISION', 'FINAL_OUTPUT'.
        """
        log_entry = {
            "timestamp": datetime.datetime.now().isoformat(),
            "event_type": event_type,
            "content": content
        }
        self._write_log_entry(log_entry)
        return log_entry

    def log_input(self, message: str | list, source: str = "N/A"):
        """Logs the initial raw input message."""
        return self.log_event("INPUT", {"message": message, "source": source})

    def log_tool_call(self, tool_name: str, tool_args: dict):
        """Logs when the agent decides to call a specific tool."""
        return self.log_event("TOOL_CALL", {"tool_name": tool_name, "arguments": tool_args})

    def log_tool_response(self, tool_name: str, response_content: dict):
        """Logs the result returned by an executed tool."""
        return self.log_event("TOOL_RESPONSE", {"tool_name": tool_name, "result": response_content})

    def log_llm_decision(self, decision_text: str, prompt_context: list):
        """Logs the raw textual decision or thought process from the LLM."""
        return self.log_event("LLM_DECISION", {"thought_process": decision_text, "context": prompt_context})

    def log_final_output(self, message: str):
        """Logs the final synthesized response given to the user/caller."""
        return self.log_event("FINAL_OUTPUT", {"message": message})

    def log_error(self, message: str, details: dict | None = None):
        """Logs an error event with optional structured details."""
        content: dict = {"message": message}
        if details:
            content["details"] = details
        return self.log_event("ERROR", content)

# Initialize a global logger instance for simplicity in high-level modules
logger = AgentLogger()
