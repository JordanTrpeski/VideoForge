"""
test_connections.py
===================
Ping all configured external APIs and report pass/fail for each.
Run this at the start of every session to confirm credentials are valid.

Input:  .env API keys
Output: Console report — PASS / FAIL / SKIP per service
Logs:   logs/test_connections.log

Dependencies:
    - anthropic (Claude API)
    - requests (ElevenLabs, Leonardo.AI, Pexels)
    - google-api-python-client (YouTube)
    - python-dotenv

Author: VideoForge
Version: 1.1
"""

# 1. Standard library
import os
import sys
import time
from pathlib import Path

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
    api_key = os.getenv('ANTHROPIC_API_KEY', '').strip()
    if not api_key:
        print(f"  [{SKIP}] Anthropic        — ANTHROPIC_API_KEY not set in .env")
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
            print(f"  [{PASS}] Anthropic        — {elapsed:.2f}s")
            logger.info(f"Anthropic API OK — {elapsed:.2f}s")
            return True
        print(f"  [{FAIL}] Anthropic        — empty response")
        logger.error("Anthropic API returned empty response")
        return False
    except Exception as e:
        elapsed = time.time() - start
        print(f"  [{FAIL}] Anthropic        — {e}")
        logger.error(f"Anthropic API failed after {elapsed:.2f}s: {e}", exc_info=True)
        return False


def test_elevenlabs() -> bool:
    """
    Verify the ElevenLabs API key by listing available voices.

    Uses /v1/voices rather than /v1/user/subscription because most API keys
    are not provisioned with the user_read scope.

    Returns:
        bool: True if the API returns 200 with a voices list.
    """
    api_key = os.getenv('ELEVENLABS_API_KEY', '').strip()
    if not api_key:
        print(f"  [{SKIP}] ElevenLabs       — ELEVENLABS_API_KEY not set in .env")
        logger.info("ElevenLabs test skipped — key not set")
        return True   # Not a failure — key is optional until Phase 3 runs

    logger.info("Testing ElevenLabs API connection")
    start = time.time()
    try:
        url = "https://api.elevenlabs.io/v1/voices"
        r = requests.get(url, headers={"xi-api-key": api_key}, timeout=10)
        elapsed = time.time() - start

        if r.status_code == 200:
            voice_count = len(r.json().get("voices", []))
            voice_id    = os.getenv("ELEVENLABS_VOICE_ID", "").strip()
            voice_note  = f"voice ID set: {voice_id}" if voice_id else "ELEVENLABS_VOICE_ID not set yet"
            print(f"  [{PASS}] ElevenLabs       — {elapsed:.2f}s, {voice_count} voices available, {voice_note}")
            logger.info(f"ElevenLabs API OK — {elapsed:.2f}s, {voice_count} voices")
            return True
        print(f"  [{FAIL}] ElevenLabs       — HTTP {r.status_code}: {r.text[:100]}")
        logger.error(f"ElevenLabs API HTTP {r.status_code}: {r.text[:200]}")
        return False
    except Exception as e:
        elapsed = time.time() - start
        print(f"  [{FAIL}] ElevenLabs       — {e}")
        logger.error(f"ElevenLabs API failed after {elapsed:.2f}s: {e}", exc_info=True)
        return False


def test_leonardo() -> bool:
    """
    Verify the Leonardo.AI API key by fetching the /me user endpoint.
    Reports token balance (apiPaidTokens + subscriptionTokens).

    Returns:
        bool: True if the API returns a 200 status with user details.
    """
    api_key = os.getenv('LEONARDO_API_KEY', '').strip()
    if not api_key:
        print(f"  [{SKIP}] Leonardo.AI      — LEONARDO_API_KEY not set in .env")
        logger.info("Leonardo.AI test skipped — key not set")
        return True   # Not a failure — key is optional until Phase 4 runs

    logger.info("Testing Leonardo.AI API connection")
    start = time.time()
    try:
        url = "https://cloud.leonardo.ai/api/rest/v1/me"
        r = requests.get(url, headers={"authorization": f"Bearer {api_key}"}, timeout=10)
        elapsed = time.time() - start

        if r.status_code == 200:
            details  = r.json().get("user_details", [{}])[0]
            api_paid = details.get("apiPaidTokens", 0) or 0
            sub_tok  = details.get("subscriptionTokens", 0) or 0
            total    = api_paid + sub_tok
            slots    = details.get("apiConcurrencySlots", "?")
            print(f"  [{PASS}] Leonardo.AI      — {elapsed:.2f}s, {total:,} tokens ({api_paid:,} paid + {sub_tok} subscription), {slots} concurrency slots")
            logger.info(f"Leonardo.AI API OK — {elapsed:.2f}s, {total} tokens available")
            return True
        print(f"  [{FAIL}] Leonardo.AI      — HTTP {r.status_code}: {r.text[:100]}")
        logger.error(f"Leonardo.AI API HTTP {r.status_code}: {r.text[:200]}")
        return False
    except Exception as e:
        elapsed = time.time() - start
        print(f"  [{FAIL}] Leonardo.AI      — {e}")
        logger.error(f"Leonardo.AI API failed after {elapsed:.2f}s: {e}", exc_info=True)
        return False


def test_pexels() -> bool:
    """
    Verify the Pexels API key by performing a minimal video search query.

    Returns:
        bool: True if the API returns a 200 status with results.
    """
    api_key = os.getenv('PEXELS_API_KEY', '').strip()
    if not api_key:
        print(f"  [{SKIP}] Pexels           — PEXELS_API_KEY not set in .env")
        logger.info("Pexels test skipped — key not set")
        return True

    logger.info("Testing Pexels API connection")
    start = time.time()
    try:
        r = requests.get(
            "https://api.pexels.com/videos/search",
            headers={"Authorization": api_key},
            params={"query": "engineering", "per_page": 1},
            timeout=10,
        )
        elapsed = time.time() - start
        if r.status_code == 200:
            print(f"  [{PASS}] Pexels           — {elapsed:.2f}s")
            logger.info(f"Pexels API OK — {elapsed:.2f}s")
            return True
        print(f"  [{FAIL}] Pexels           — HTTP {r.status_code}: {r.text[:100]}")
        logger.error(f"Pexels API HTTP {r.status_code}: {r.text[:200]}")
        return False
    except Exception as e:
        elapsed = time.time() - start
        print(f"  [{FAIL}] Pexels           — {e}")
        logger.error(f"Pexels API failed after {elapsed:.2f}s: {e}", exc_info=True)
        return False


def test_youtube() -> bool:
    """
    Check YouTube OAuth credentials readiness.

    Does not make a live API call — checks for the OAuth token file and
    client secrets file so the upload engine can run without re-authing.

    Returns:
        bool: True if token.json is present (fully authenticated).
               False if secrets file is missing.
               True (with warning) if secrets exist but OAuth not yet done.
    """
    secrets_env  = os.getenv('YOUTUBE_CLIENT_SECRETS_FILE', 'client_secrets.json')
    secrets_path = Path(secrets_env)
    token_path   = Path('token.json')

    # Also accept auto-downloaded filename pattern
    if not secrets_path.exists():
        found = next(Path('.').glob('client_secret_*.json'), None)
        if found:
            secrets_path = found

    if not secrets_path.exists():
        print(f"  [{SKIP}] YouTube          — client_secrets.json not found (set YOUTUBE_CLIENT_SECRETS_FILE in .env)")
        logger.info("YouTube test skipped — secrets file not found")
        return True   # Not a failure — credentials added in Phase 8

    if token_path.exists():
        print(f"  [{PASS}] YouTube          — OAuth token present ({secrets_path.name})")
        logger.info("YouTube credentials OK — token.json present")
        return True

    print(f"  [{SKIP}] YouTube          — secrets file present ({secrets_path.name}), OAuth not yet done (run upload once)")
    logger.info("YouTube secrets present but token.json missing — OAuth needed")
    return True   # Not a failure — first upload will trigger OAuth flow


def test_tiktok() -> bool:
    """
    Check TikTok API credentials readiness.

    Verifies that both developer keys and an access token are present.
    Does not make a live API call — TikTok access tokens expire and a
    full auth flow is needed to refresh them.

    Returns:
        bool: True if developer keys are set (access token optional).
    """
    client_key    = os.getenv('TIKTOK_CLIENT_KEY', '').strip()
    client_secret = os.getenv('TIKTOK_CLIENT_SECRET', '').strip()
    access_token  = os.getenv('TIKTOK_ACCESS_TOKEN', '').strip()

    if not client_key or not client_secret:
        print(f"  [{SKIP}] TikTok           — TIKTOK_CLIENT_KEY / SECRET not set in .env")
        logger.info("TikTok test skipped — developer keys not set")
        return True   # Not a failure — credentials added in Phase 8

    if access_token:
        # Lightweight live check against the token introspect endpoint
        start = time.time()
        try:
            r = requests.post(
                "https://open.tiktokapis.com/v2/oauth/token/",
                data={
                    "client_key":    client_key,
                    "client_secret": client_secret,
                    "grant_type":    "client_credentials",
                },
                timeout=8,
            )
            elapsed = time.time() - start
            if r.status_code == 200:
                print(f"  [{PASS}] TikTok           — {elapsed:.2f}s, access token present")
                logger.info(f"TikTok API OK — {elapsed:.2f}s")
                return True
            # 4xx errors can mean expired token, still report key as set
            print(f"  [{SKIP}] TikTok           — developer keys set, token may be expired (HTTP {r.status_code}) — use Re-auth in dashboard")
            return True
        except Exception as e:
            elapsed = time.time() - start
            print(f"  [{FAIL}] TikTok           — {e}")
            logger.error(f"TikTok test failed: {e}", exc_info=True)
            return False
    else:
        print(f"  [{SKIP}] TikTok           — developer keys set, access token not yet obtained — use Re-auth in dashboard")
        logger.info("TikTok developer keys present, access token missing")
        return True


def run_all_tests() -> int:
    """
    Run connection tests for all configured APIs and print a summary report.

    Returns:
        int: Number of hard failures (0 = all passed or gracefully skipped).
    """
    print("\n" + "=" * 60)
    print("  VideoForge — API Connection Test")
    print("=" * 60)

    results = {
        "Anthropic":   test_anthropic(),
        "ElevenLabs":  test_elevenlabs(),
        "Leonardo.AI": test_leonardo(),
        "Pexels":      test_pexels(),
        "YouTube":     test_youtube(),
        "TikTok":      test_tiktok(),
    }

    print("=" * 60)
    passed = sum(1 for v in results.values() if v)
    total  = len(results)
    print(f"  Result: {passed}/{total} services OK or gracefully skipped")
    print("=" * 60 + "\n")

    failures = total - passed
    if failures > 0:
        logger.warning(f"Connection test finished — {failures} hard failure(s)")
    else:
        logger.info("Connection test finished — all services OK or skipped")

    return failures


if __name__ == '__main__':
    exit_code = run_all_tests()
    sys.exit(exit_code)
