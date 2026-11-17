"""Shared configuration: Gemini client creation.

The API key is read from the environment (GEMINI_API_KEY, or GOOGLE_API_KEY)
and the client is created lazily, so importing this package never needs a key
and a key is never stored in source control.
"""
import os

MODEL_NAME = "gemini-2.5-flash"

_client = None


class MissingAPIKeyError(RuntimeError):
    pass


def get_client():
    """Return a cached google-genai client, or raise MissingAPIKeyError."""
    global _client
    if _client is None:
        api_key = os.environ.get("GEMINI_API_KEY") or os.environ.get("GOOGLE_API_KEY")
        if not api_key:
            raise MissingAPIKeyError(
                "No Gemini API key found. Set the GEMINI_API_KEY environment variable "
                "(e.g. in PowerShell: $env:GEMINI_API_KEY = 'your-key') and restart the app."
            )
        from google import genai  # imported lazily so tests don't need the SDK
        _client = genai.Client(api_key=api_key)
    return _client
