"""
main.py
=======
CLI entry point for the VideoForge pipeline.
Every pipeline stage can be triggered individually or as a full run.

Input:  Command-line arguments
Output: Delegates to the relevant module(s)
Logs:   logs/main.log

Dependencies:
    - argparse (stdlib)
    - json (stdlib)

Author: VideoForge
Version: 1.0
"""

# 1. Standard library
import argparse
import json
import os
import sys
from pathlib import Path

# 2. Third-party libraries
from dotenv import load_dotenv

load_dotenv()

# Ensure project root is importable regardless of working directory
sys.path.insert(0, str(Path(__file__).parent))


def load_config() -> dict:
    """
    Load config.json from the project root.

    Returns:
        dict: Parsed configuration dictionary.

    Raises:
        FileNotFoundError: If config.json does not exist.
    """
    config_path = Path('config.json')
    if not config_path.exists():
        print("ERROR: config.json not found. Run from the VideoForge project root.", file=sys.stderr)
        sys.exit(1)
    with open(config_path, 'r', encoding='utf-8') as f:
        return json.load(f)


# ---------------------------------------------------------------------------
# Command handlers
# ---------------------------------------------------------------------------

def cmd_generate_script(args) -> None:
    """
    Run Stage 1: generate a script for a single topic.

    Args:
        args: Parsed argparse namespace with topic, bucket, hook attributes.
    """
    from database import init_db, create_job, get_next_job_id
    from modules.script_engine import generate_script

    config = load_config()
    init_db()

    job_id = get_next_job_id()
    bucket = args.bucket or 'elec'
    hook_style = args.hook or config['script']['hook_style']

    create_job(job_id=job_id, topic=args.topic, bucket=bucket, hook_style=hook_style)

    print(f"\nJob {job_id} created — topic: '{args.topic}'")
    print(f"Bucket: {bucket} | Hook: {hook_style}\n")

    result = generate_script(
        job_id=job_id,
        topic=args.topic,
        config=config,
        bucket=bucket,
        hook_style=hook_style
    )

    if result['success']:
        print(f"\nScript saved to: {result['output_path']}")

        # Print a preview of the generated script
        with open(result['output_path'], 'r', encoding='utf-8') as f:
            data = json.load(f)
        print(f"\n--- NARRATION PREVIEW ({data['word_count']} words, ~{data['estimated_duration_seconds']}s) ---")
        print(data['narration'])
        print(f"\n--- VISUAL BRIEF ({len(data['visual_brief'])} prompts) ---")
        for i, prompt in enumerate(data['visual_brief'], 1):
            print(f"  [{i}] {prompt}")
        print()
    else:
        print(f"\nERROR: Script generation failed — {result['error']}", file=sys.stderr)
        sys.exit(1)


def cmd_generate_voice(args) -> None:
    """
    Run Stage 2: synthesise speech for an existing job's script.

    Args:
        args: Parsed argparse namespace with job_id attribute.
    """
    from database import init_db, get_job
    from modules.voice_engine import generate_voice

    config = load_config()
    init_db()

    job = get_job(args.job_id)
    if not job:
        print(f"ERROR: Job {args.job_id} not found. Run generate-script first.", file=sys.stderr)
        sys.exit(1)

    if not job.get('script_path'):
        print(
            f"ERROR: Job {args.job_id} has no script yet (status: {job['status']}). "
            "Run generate-script first.",
            file=sys.stderr
        )
        sys.exit(1)

    print(f"\nJob {args.job_id} — generating voice for: '{job['topic']}'")

    result = generate_voice(job_id=args.job_id, config=config)

    if result.get('skipped'):
        print(f"\nSKIPPED: {result['error']}")
        print("Set ELEVENLABS_API_KEY and ELEVENLABS_VOICE_ID in .env to enable voice generation.")
    elif result['success']:
        print(f"\nAudio saved to:   {result['output_path']}")
        print(f"Duration:         {result['duration_seconds']}s")
    else:
        print(f"\nERROR: Voice generation failed — {result['error']}", file=sys.stderr)
        sys.exit(1)


def cmd_test_connections(args) -> None:
    """
    Run the API connection test suite.

    Args:
        args: Parsed argparse namespace (no specific fields required).
    """
    import subprocess
    result = subprocess.run(
        [sys.executable, 'tests/test_connections.py'],
        capture_output=False
    )
    sys.exit(result.returncode)


def cmd_status(args) -> None:
    """
    Print the current status of a job.

    Args:
        args: Parsed argparse namespace with job_id attribute.
    """
    from database import init_db, get_job
    init_db()

    job = get_job(args.job_id)
    if not job:
        print(f"Job {args.job_id} not found.", file=sys.stderr)
        sys.exit(1)

    print(f"\n--- JOB {job['id']} ---")
    print(f"Topic:   {job['topic']}")
    print(f"Bucket:  {job['bucket']}")
    print(f"Hook:    {job['hook_style']}")
    print(f"Status:  {job['status']}")
    if job['error_message']:
        print(f"Error:   [{job['error_module']}] {job['error_message']}")
    print(f"Created: {job['created_at']}")
    print(f"Updated: {job['updated_at']}")
    for field in ('script_path', 'audio_path', 'images_dir', 'raw_video_path',
                  'final_video_path', 'thumbnail_path', 'metadata_path',
                  'youtube_url', 'tiktok_url'):
        if job.get(field):
            print(f"  {field}: {job[field]}")
    print()


def cmd_list_jobs(args) -> None:
    """
    Print a table of all jobs in the database.

    Args:
        args: Parsed argparse namespace (no specific fields required).
    """
    from database import init_db, get_all_jobs
    init_db()

    jobs = get_all_jobs()
    if not jobs:
        print("No jobs found.")
        return

    print(f"\n{'ID':<6} {'STATUS':<14} {'BUCKET':<8} {'CREATED':<20} TOPIC")
    print("-" * 80)
    for job in jobs:
        topic = job['topic'][:45] + ('…' if len(job['topic']) > 45 else '')
        print(f"{job['id']:<6} {job['status']:<14} {job.get('bucket') or '':<8} "
              f"{job['created_at']:<20} {topic}")
    print()


# ---------------------------------------------------------------------------
# Argument parser
# ---------------------------------------------------------------------------

def build_parser() -> argparse.ArgumentParser:
    """
    Build and return the top-level argument parser with all subcommands.

    Returns:
        argparse.ArgumentParser: Configured parser.
    """
    parser = argparse.ArgumentParser(
        prog='main.py',
        description='VideoForge — AI video production pipeline'
    )
    subparsers = parser.add_subparsers(dest='command', metavar='COMMAND')
    subparsers.required = True

    # test-connections
    subparsers.add_parser('test-connections', help='Ping all configured APIs')

    # generate-script
    p_script = subparsers.add_parser('generate-script', help='Run Stage 1: generate script')
    p_script.add_argument('topic', type=str, help='Video topic e.g. "Why phone chargers get warm"')
    p_script.add_argument('--bucket', type=str, choices=['elec', 'infra', 'vehicle', 'flaw'],
                          default='elec', help='Content bucket (default: elec)')
    p_script.add_argument('--hook', type=str,
                          choices=['shocking_fact', 'wrong_assumption', 'nobody_talks'],
                          default=None,
                          help='Hook style override (default: value from config.json)')

    # generate-voice
    p_voice = subparsers.add_parser('generate-voice', help='Run Stage 2: synthesise speech')
    p_voice.add_argument('job_id', type=str, help='Job ID e.g. 001')

    # status
    p_status = subparsers.add_parser('status', help='Show status of a single job')
    p_status.add_argument('job_id', type=str, help='Job ID e.g. 001')

    # list-jobs
    subparsers.add_parser('list-jobs', help='List all jobs')

    return parser


COMMAND_MAP = {
    'test-connections': cmd_test_connections,
    'generate-script':  cmd_generate_script,
    'generate-voice':   cmd_generate_voice,
    'status':           cmd_status,
    'list-jobs':        cmd_list_jobs,
}


def main() -> None:
    """
    Parse CLI arguments and dispatch to the correct command handler.
    """
    parser = build_parser()
    args = parser.parse_args()

    handler = COMMAND_MAP.get(args.command)
    if not handler:
        parser.print_help()
        sys.exit(1)

    handler(args)


if __name__ == '__main__':
    main()
