"""
test_connections.py
===================
Ping all configured external APIs and report pass/fail for each.
Run this at the start of every session to confirm credentials are valid.

Input:  .env API keys
Output: Console report — PASS / FAIL per service
Logs:   logs/test_connections.log

Dependencies:
    - anthropic (Claude API)
    - requests (ElevenLabs, Leonardo.AI, Pexels)
    - google-api-python-client (YouTube)
    - python-dotenv

Author: VideoForge
Version: 1.0
"""

# 1. Standard library
import os
import sys
import time

# 2. Third-party libraries
import requests
from dotenv import load_dotenv

# Ensure project root is on path when running from tests/ directory
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

# 3. Local modules
from utils.logger import setup_logger

load_dotenv()
logger = setup_logger('test_connections')

PASS = "\033[92mPASS\033[0m"
FAIL = "\033[91mFAIL\033[0m"
SKIP = "\033[93mSKIP\033[0m"


def test_anthropic() -> bool:
    """
    Verify the Anthropic API key by sending a minimal one-token message.

    Returns:
        bool: True if the API responds with a valid message object.
    """
    api_key = os.getenv('ANTHROPIC_API_KEY')
    if not api_key:
        print(f"  [{SKIP}] Anthropic — ANTHROPIC_API_KEY not set in .env")
        logger.warning("Anthropic test skipped — key not set")
        return False

    logger.info("Testing Anthropic API connection")
    start = time.time()
    try:
        import anthropic
        client = anthropic.Anthropic(api_key=api_key)
        message = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=10,
            messages=[{"role": "user", "content": "Say OK"}]
        )
        elapsed = time.time() - start
        if message and message.content:
            print(f"  [{PASS}] Anthropic — response time: {elapsed:.2f}s")
            logger.info(f"Anthropic API OK — response time: {elapsed:.2f}s")
            return True
        else:
            print(f"  [{FAIL}] Anthropic — empty response")
            logger.error("Anthropic API returned empty response")
            return False
    except Exception as e:
        elapsed = time.time() - start
        print(f"  [{FAIL}] Anthropic — {e}")
        logger.error(f"Anthropic API failed after {elapsed:.2f}s: {e}", exc_info=True)
        return False


def test_elevenlabs() -> bool:
    """
    Verify the ElevenLabs API key by fetching the user subscription info.

    Returns:
        bool: True if the API returns a 200 status with subscription data.
    """
    api_key = os.getenv('ELEVENLABS_API_KEY')
    if not api_key:
        print(f"  [{SKIP}] ElevenLabs — ELEVENLABS_API_KEY not set in .env (expected — Phase 3)")
        logger.info("ElevenLabs test skipped — key not set (Phase 3 dependency)")
        return True  # Not a failure — key is added in Phase 3

    logger.info("Testing ElevenLabs API connection")
    start = time.time()
    try:
        url = "https://api.elevenlabs.io/v1/user/subscription"
        headers = {"xi-api-key": api_key}
        response = requests.get(url, headers=headers, timeout=10)
        elapsed = time.time() - start

        if response.status_code == 200:
            data = response.json()
            chars_remaining = data.get('character_limit', 0) - data.get('character_count', 0)
            print(f"  [{PASS}] ElevenLabs — response time: {elapsed:.2f}s, chars remaining: {chars_remaining:,}")
            logger.info(f"ElevenLabs API OK — {elapsed:.2f}s, chars remaining: {chars_remaining}")
            return True
        else:
            print(f"  [{FAIL}] ElevenLabs — HTTP {response.status_code}: {response.text[:100]}")
            logger.error(f"ElevenLabs API HTTP {response.status_code}: {response.text[:200]}")
            return False
    except Exception as e:
        elapsed = time.time() - start
        print(f"  [{FAIL}] ElevenLabs — {e}")
        logger.error(f"ElevenLabs API failed after {elapsed:.2f}s: {e}", exc_info=True)
        return False


def test_leonardo() -> bool:
    """
    Verify the Leonardo.AI API key by fetching the user info endpoint.

    Returns:
        bool: True if the API returns a 200 status.
    """
    api_key = os.getenv('LEONARDO_API_KEY')
    if not api_key:
        print(f"  [{SKIP}] Leonardo.AI — LEONARDO_API_KEY not set in .env (expected — Phase 4)")
        logger.info("Leonardo.AI test skipped — key not set (Phase 4 dependency)")
        return True  # Not a failure — key is added in Phase 4

    logger.info("Testing Leonardo.AI API connection")
    start = time.time()
    try:
        url = "https://cloud.leonardo.ai/api/rest/v1/me"
        headers = {"Authorization": f"Bearer {api_key}"}
        response = requests.get(url, headers=headers, timeout=10)
        elapsed = time.time() - start

        if response.status_code == 200:
            data = response.json()
            user_info = data.get('user_details', [{}])
            credits = user_info[0].get('apiConcurrencySlots', 'N/A') if user_info else 'N/A'
            print(f"  [{PASS}] Leonardo.AI — response time: {elapsed:.2f}s")
            logger.info(f"Leonardo.AI API OK — {elapsed:.2f}s")
            return True
        else:
            print(f"  [{FAIL}] Leonardo.AI — HTTP {response.status_code}: {response.text[:100]}")
            logger.error(f"Leonardo.AI API HTTP {response.status_code}: {response.text[:200]}")
            return False
    except Exception as e:
        elapsed = time.time() - start
        print(f"  [{FAIL}] Leonardo.AI — {e}")
        logger.error(f"Leonardo.AI API failed after {elapsed:.2f}s: {e}", exc_info=True)
        return False


def test_pexels() -> bool:
    """
    Verify the Pexels API key by performing a test search query.

    Returns:
        bool: True if the API returns a 200 status with results.
    """
    api_key = os.getenv('PEXELS_API_KEY')
    if not api_key:
        print(f"  [{SKIP}] Pexels — PEXELS_API_KEY not set in .env (expected — Phase 4)")
        logger.info("Pexels test skipped — key not set (Phase 4 dependency)")
        return True  # Not a failure — key is added in Phase 4

    logger.info("Testing Pexels API connection")
    start = time.time()
    try:
        url = "https://api.pexels.com/videos/search"
        headers = {"Authorization": api_key}
        params = {"query": "engineering", "per_page": 1}
        response = requests.get(url, headers=headers, params=params, timeout=10)
        elapsed = time.time() - start

        if response.status_code == 200:
            print(f"  [{PASS}] Pexels — response time: {elapsed:.2f}s")
            logger.info(f"Pexels API OK — {elapsed:.2f}s")
            return True
        else:
            print(f"  [{FAIL}] Pexels — HTTP {response.status_code}: {response.text[:100]}")
            logger.error(f"Pexels API HTTP {response.status_code}: {response.text[:200]}")
            return False
    except Exception as e:
        elapsed = time.time() - start
        print(f"  [{FAIL}] Pexels — {e}")
        logger.error(f"Pexels API failed after {elapsed:.2f}s: {e}", exc_info=True)
        return False


def run_all_tests() -> int:
    """
    Run connection tests for all configured APIs and print a summary report.

    Returns:
        int: Number of tests that failed (0 = all passed or skipped).
    """
    print("\n" + "=" * 55)
    print("  VideoForge — API Connection Test")
    print("=" * 55)

    results = {
        "Anthropic": test_anthropic(),
        "ElevenLabs": test_elevenlabs(),
        "Leonardo.AI": test_leonardo(),
        "Pexels": test_pexels(),
    }

    print("=" * 55)
    passed = sum(1 for v in results.values() if v)
    total = len(results)
    print(f"  Result: {passed}/{total} services OK")
    print("=" * 55 + "\n")

    failures = total - passed
    if failures > 0:
        logger.warning(f"Connection test finished — {failures} service(s) failed")
    else:
        logger.info("Connection test finished — all services OK")

    return failures


if __name__ == '__main__':
    exit_code = run_all_tests()
    sys.exit(exit_code)
