import os

# Set VERBOSE=1 in .env or pass --verbose on the CLI to enable verbose LLM tracing.
verbose: bool = os.getenv("VERBOSE", "0") == "1"
