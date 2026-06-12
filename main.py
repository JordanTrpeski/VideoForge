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


def load_config(channel_slug: str = None) -> dict:
    """
    Load the fully-merged config for the given channel (or the default channel).

    Falls back to raw config.json if the config_loader is unavailable.

    Args:
        channel_slug (str): Channel identifier. Uses default_channel from
                            config.json when None.

    Returns:
        dict: Merged configuration dictionary.
    """
    try:
        from utils.config_loader import load_channel_config, get_default_channel
        slug = channel_slug or get_default_channel()
        return load_channel_config(slug)
    except Exception:
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

    channel = getattr(args, 'channel', None)
    config = load_config(channel_slug=channel)
    init_db()

    job_id = get_next_job_id()
    bucket = args.bucket or 'elec'
    hook_style = args.hook or config['script']['hook_style']
    channel_id = channel or config.get('default_channel', 'engineering_brief')

    create_job(job_id=job_id, topic=args.topic, bucket=bucket, hook_style=hook_style,
               channel_id=channel_id)

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


def cmd_generate_images(args) -> None:
    """
    Run Stage 3: generate images for an existing job's visual_brief.

    Args:
        args: Parsed argparse namespace with job_id attribute.
    """
    from database import init_db, get_job
    from modules.image_engine import generate_images

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

    print(f"\nJob {args.job_id} — generating images for: '{job['topic']}'")

    result = generate_images(job_id=args.job_id, config=config)

    if result.get('skipped'):
        print(f"\nSKIPPED: {result['error']}")
        print("Set LEONARDO_API_KEY in .env to enable image generation.")
    elif result['success']:
        print(f"\nImages saved to: {result['images_dir']}")
        print(f"Count:           {result['count']} images")
    else:
        print(f"\nERROR: Image generation failed — {result['error']}", file=sys.stderr)
        sys.exit(1)


def cmd_assemble(args) -> None:
    """
    Run Stage 4: assemble raw MP4 from images and audio for an existing job.

    Args:
        args: Parsed argparse namespace with job_id attribute.
    """
    from database import init_db, get_job
    from modules.assembly_engine import assemble_video

    config = load_config()
    init_db()

    job = get_job(args.job_id)
    if not job:
        print(f"ERROR: Job {args.job_id} not found. Run generate-script first.", file=sys.stderr)
        sys.exit(1)

    print(f"\nJob {args.job_id} — assembling video for: '{job['topic']}'")

    result = assemble_video(job_id=args.job_id, config=config)

    if result['success']:
        print(f"\nRaw video saved to: {result['output_path']}")
    else:
        print(f"\nERROR: Assembly failed — {result['error']}", file=sys.stderr)
        sys.exit(1)


def cmd_add_captions(args) -> None:
    """
    Run Stage 5: transcribe audio and burn captions into the raw video.

    Args:
        args: Parsed argparse namespace with job_id attribute.
    """
    from database import init_db, get_job
    from modules.caption_engine import add_captions

    config = load_config()
    init_db()

    job = get_job(args.job_id)
    if not job:
        print(f"ERROR: Job {args.job_id} not found.", file=sys.stderr)
        sys.exit(1)

    if not job.get('raw_video_path'):
        print(
            f"ERROR: Job {args.job_id} has no raw video yet (status: {job['status']}). "
            "Run assemble first.",
            file=sys.stderr
        )
        sys.exit(1)

    print(f"\nJob {args.job_id} — adding captions for: '{job['topic']}'")

    result = add_captions(job_id=args.job_id, config=config)

    if result['success']:
        print(f"\nCaptioned video: {result['output_path']}")
        print(f"Caption blocks:  {result['caption_count']}")
    else:
        print(f"\nERROR: Caption engine failed — {result['error']}", file=sys.stderr)
        sys.exit(1)


def cmd_generate_metadata(args) -> None:
    """
    Run Stage 6: generate SEO metadata for an existing job.

    Args:
        args: Parsed argparse namespace with job_id attribute.
    """
    from database import init_db, get_job
    from modules.metadata_engine import generate_metadata

    config = load_config()
    init_db()

    job = get_job(args.job_id)
    if not job:
        print(f"ERROR: Job {args.job_id} not found.", file=sys.stderr)
        sys.exit(1)

    print(f"\nJob {args.job_id} — generating metadata for: '{job['topic']}'")

    result = generate_metadata(job_id=args.job_id, config=config)

    if result['success']:
        print(f"\nMetadata saved to: {result['output_path']}")
        with open(result['output_path'], 'r', encoding='utf-8') as f:
            data = json.load(f)
        print(f"  TikTok title:    {data['tiktok_title']}")
        print(f"  YouTube title:   {data['youtube_title']}")
        print(f"  Thumbnail text:  {data['thumbnail_text']}")
        print(f"  Hashtags ({len(data['tiktok_hashtags'])}):    {' '.join(data['tiktok_hashtags'][:5])} ...")
    else:
        print(f"\nERROR: Metadata generation failed — {result['error']}", file=sys.stderr)
        sys.exit(1)


def cmd_generate_thumbnail(args) -> None:
    """
    Run Stage 6b: capture a frame and generate the thumbnail for an existing job.

    Args:
        args: Parsed argparse namespace with job_id attribute.
    """
    from database import init_db, get_job
    from modules.thumbnail_engine import generate_thumbnail

    config = load_config()
    init_db()

    job = get_job(args.job_id)
    if not job:
        print(f"ERROR: Job {args.job_id} not found.", file=sys.stderr)
        sys.exit(1)

    print(f"\nJob {args.job_id} — generating thumbnail for: '{job['topic']}'")

    result = generate_thumbnail(job_id=args.job_id, config=config)

    if result['success']:
        print(f"\nThumbnail saved to: {result['output_path']}")
    else:
        print(f"\nERROR: Thumbnail generation failed — {result['error']}", file=sys.stderr)
        sys.exit(1)


def cmd_upload(args) -> None:
    """
    Run Stage 7: upload captioned video to YouTube Shorts and TikTok.

    Args:
        args: Parsed argparse namespace with job_id attribute.
    """
    from database import init_db, get_job
    from modules.upload_engine import upload_video

    config = load_config()
    init_db()

    job = get_job(args.job_id)
    if not job:
        print(f"ERROR: Job {args.job_id} not found.", file=sys.stderr)
        sys.exit(1)

    if not job.get('final_video_path') and not job.get('raw_video_path'):
        print(
            f"ERROR: Job {args.job_id} has no video yet (status: {job['status']}). "
            "Run assemble and add-captions first.",
            file=sys.stderr
        )
        sys.exit(1)

    print(f"\nJob {args.job_id} — uploading video for: '{job['topic']}'")

    result = upload_video(job_id=args.job_id, config=config)

    yt = result.get('youtube', {})
    tt = result.get('tiktok', {})

    if yt.get('skipped'):
        print(f"\nYouTube:  SKIPPED — {yt['error']}")
    elif yt.get('success'):
        print(f"\nYouTube:  {yt['url']}")
    else:
        print(f"\nYouTube:  FAILED — {yt.get('error', 'unknown error')}", file=sys.stderr)

    if tt.get('skipped'):
        print(f"TikTok:   SKIPPED — {tt['error']}")
    elif tt.get('success'):
        print(f"TikTok:   {tt['url']}")
    else:
        print(f"TikTok:   FAILED — {tt.get('error', 'unknown error')}", file=sys.stderr)

    if not result.get('success') and not result.get('all_skipped'):
        sys.exit(1)

    print()


def cmd_batch(args) -> None:
    """
    Manually trigger a batch run of N jobs from the queue without waiting
    for the Sunday schedule. Picks the oldest N queued jobs and runs the full
    pipeline for each in sequence, stopping at the review gate.

    Args:
        args: Parsed argparse namespace with count attribute.
    """
    from database import init_db
    from scheduler import run_batch

    init_db()
    count = args.count
    channel = getattr(args, 'channel', None)

    print(f"\nVideoForge — manual batch run ({count} job(s))")
    if channel:
        print(f"Channel: {channel}")
    print("=" * 50)

    summary = run_batch(count=count, channel_id=channel)

    print("=" * 50)
    print(f"Batch complete:")
    print(f"  Jobs attempted : {summary['total']}")
    print(f"  Succeeded      : {summary['succeeded']}")
    print(f"  Failed         : {summary['failed']}")
    print(f"  Total time     : {summary['elapsed']}s")

    if summary['succeeded'] > 0:
        print(f"\nJobs that succeeded are now at 'review' status.")
        print("Open the dashboard (python app.py) to approve and upload.")

    if summary['failed'] > 0:
        print(f"\nFailed jobs: check logs/errors.log for details.")
        sys.exit(1)

    print()


def cmd_scan_trends(args) -> None:
    """
    Run a Google Trends scan immediately, create priority alerts for any
    engineering topics spiking above the configured threshold.

    Args:
        args: Parsed argparse namespace (no specific fields required).
    """
    from database import init_db
    from modules.trend_monitor import run_scan

    config = load_config()
    init_db()

    print("\nVideoForge — running trend scan")
    print("=" * 50)

    result = run_scan(config)

    if result.get('blocked'):
        print(f"\nSCAN BLOCKED: {result['reason']}")
        sys.exit(0)

    if not result['success']:
        print(f"\nSCAN FAILED: {result.get('reason', 'unknown error')}", file=sys.stderr)
        sys.exit(1)

    print(f"Spikes found above threshold : {result['topics_found']}")
    print(f"Priority alerts created      : {result['new_alerts']}")

    if result['alerts']:
        print("\nNew alerts:")
        for a in result['alerts']:
            print(f"  [{a['channel_fit']}/10] {a['topic']}")
            print(f"    Reframed: {a['reframed_angle']}")
            print(f"    Hook:     {a['hook_suggestion']}")
            print(f"    Spike:    {a['spike_percent']:.0f}%")
            print(f"    Expires:  {a['expires_at']}")
    else:
        print("\nNo new alerts created (no spikes above threshold or fit minimum).")

    print()


def cmd_scan_reddit(args) -> None:
    """
    Scan subreddits for top weekly text posts and store story candidates.

    Skips gracefully (exit 0) when Reddit credentials are not configured, the
    same way the voice and image engines skip without their keys.

    Args:
        args: Parsed argparse namespace with subs, min_upvotes, limit attributes.
    """
    from database import init_db
    from modules.reddit_engine import scan_reddit

    config = load_config()
    init_db()

    # Subs come from --subs (comma-separated) or fall back to config defaults
    if args.subs:
        subs = [s.strip() for s in args.subs.split(',') if s.strip()]
    else:
        subs = config.get('reddit', {}).get('default_subs', [])

    min_upvotes = args.min_upvotes if args.min_upvotes is not None \
        else config.get('reddit', {}).get('min_upvotes', 2000)
    limit = args.limit if args.limit is not None \
        else config.get('reddit', {}).get('scan_limit', 25)

    if not subs:
        print("ERROR: No subreddits given. Use --subs aita,tifu or set reddit.default_subs in config.json.",
              file=sys.stderr)
        sys.exit(1)

    print("\nVideoForge — Reddit story scan")
    print("=" * 50)
    print(f"Subreddits:  {', '.join(subs)}")
    print(f"Min upvotes: {min_upvotes}")
    print(f"Limit/sub:   {limit}\n")

    result = scan_reddit(subs=subs, min_upvotes=min_upvotes, limit=limit)

    if result.get('skipped'):
        print(f"SKIPPED: {result['error']}")
        print("\nSet REDDIT_CLIENT_ID, REDDIT_CLIENT_SECRET and REDDIT_USER_AGENT "
              "in .env to enable Reddit scanning.")
        sys.exit(0)

    if not result['success']:
        print(f"SCAN FAILED: {result.get('error', 'unknown error')}", file=sys.stderr)
        sys.exit(1)

    print(f"Posts examined:    {result['examined']}")
    print(f"Candidates added:  {result['added']}")

    if result['candidates']:
        print("\nNew candidates (approve them in the dashboard):")
        for c in result['candidates']:
            print(f"  [{c['upvotes']:>6} up] r/{c['subreddit']} — {c['title'][:70]}")
    else:
        print("\nNo new candidates (none matched the filters or all already captured).")

    print()


def cmd_list_alerts(args) -> None:
    """
    Print all active (non-expired, non-dismissed) priority alerts.

    Args:
        args: Parsed argparse namespace (no specific fields required).
    """
    from database import init_db, get_active_alerts
    init_db()

    alerts = get_active_alerts()
    if not alerts:
        print("No active priority alerts.")
        return

    print(f"\n{'ID':<5} {'FIT':<5} {'SPIKE':<8} {'EXPIRES':<20} TOPIC")
    print("-" * 80)
    for a in alerts:
        expires = (a.get('expires_at') or '')[:16]
        print(
            f"{a['id']:<5} {a['channel_fit']:<5.1f} "
            f"{a['spike_percent']:<8.0f} {expires:<20} {a['topic']}"
        )
        if a.get('reframed_angle'):
            print(f"       Angle: {a['reframed_angle']}")
    print()


def cmd_fast_track(args) -> None:
    """
    Fast-track a priority alert: create a job and start the pipeline immediately.

    Args:
        args: Parsed argparse namespace with alert_id attribute.
    """
    from database import init_db, get_active_alerts, create_job, get_next_job_id, link_alert_to_job
    import threading

    channel = getattr(args, 'channel', None)
    config = load_config(channel_slug=channel)
    init_db()

    alerts = get_active_alerts()
    alert = next((a for a in alerts if a['id'] == args.alert_id), None)

    if not alert:
        print(
            f"ERROR: Alert {args.alert_id} not found or no longer active.",
            file=sys.stderr,
        )
        sys.exit(1)

    job_id = get_next_job_id()
    bucket = alert.get('bucket', 'elec')
    topic  = alert.get('reframed_angle') or alert['topic']
    channel_id = channel or config.get('default_channel', 'engineering_brief')

    create_job(job_id=job_id, topic=topic, bucket=bucket, hook_style='shocking_fact',
               channel_id=channel_id)
    link_alert_to_job(alert['id'], job_id)

    print(f"\nJob {job_id} created from alert {args.alert_id}")
    print(f"Topic:  {topic}")
    print(f"Bucket: {bucket}")
    print("\nRunning pipeline now (stops at review gate)...")
    print("=" * 50)

    from scheduler import run_pipeline_sync
    ok = run_pipeline_sync(job_id, config)

    if ok:
        print(f"\nJob {job_id} is now at 'review' status.")
        print("Open the dashboard to approve and upload.")
    else:
        print(f"\nPipeline failed for job {job_id}. Check logs/errors.log.")
        sys.exit(1)
    print()


def cmd_add_topic(args) -> None:
    """
    Add a topic to the topic bank without scoring it.

    Args:
        args: Parsed argparse namespace with topic and bucket attributes.
    """
    from database import init_db, insert_topic
    init_db()

    channel = getattr(args, 'channel', None) or 'engineering_brief'
    topic_id = insert_topic(
        topic=args.topic,
        bucket=args.bucket or '',
        notes=args.notes or '',
        channel_id=channel,
    )
    print(f"\nTopic added to bank — ID: {topic_id}")
    print(f"  Topic:   {args.topic}")
    print(f"  Bucket:  {args.bucket or '(not set)'}")
    print(f"  Channel: {channel}")
    print()


def cmd_archive_topic(args) -> None:
    """
    Archive a topic in the topic bank.

    Args:
        args: Parsed argparse namespace with id and reason attributes.
    """
    from database import init_db, archive_topic
    init_db()

    archive_topic(topic_id=args.id, reason=args.reason or '')
    print(f"\nTopic {args.id} archived.")
    if args.reason:
        print(f"  Reason: {args.reason}")
    print()


def cmd_export_topics(args) -> None:
    """
    Export the topic bank to a CSV file.

    Args:
        args: Parsed argparse namespace with output attribute.
    """
    import csv
    from database import init_db, get_topics
    init_db()

    topics = get_topics(include_archived=True)
    output_path = args.output or 'topics_export.csv'

    with open(output_path, 'w', newline='', encoding='utf-8') as f:
        writer = csv.DictWriter(f, fieldnames=[
            'id', 'topic', 'bucket', 'score', 'status', 'hook_suggestion',
            'notes', 'archived', 'archived_at', 'archive_reason', 'added_at',
        ])
        writer.writeheader()
        for t in topics:
            writer.writerow({k: t.get(k, '') for k in writer.fieldnames})

    print(f"\nExported {len(topics)} topics to: {output_path}")
    print()


def cmd_mine_comments(args) -> None:
    """
    Trigger YouTube comment mining and print a summary.

    Args:
        args: Parsed argparse namespace (no specific fields required).
    """
    from database import init_db
    from modules.comment_miner import mine_comments
    init_db()
    config = load_config()

    print("\nMining YouTube comments for topic ideas…")
    result = mine_comments(config)

    if not result.get('success'):
        print(f"  ERROR: {result.get('error', 'Unknown error')}")
        return

    print(f"\n  Videos scanned:   {result['videos_scanned']}")
    print(f"  Comments pulled:  {result['comments_pulled']}")
    print(f"  Topics added:     {result['topics_added']}")
    if result.get('topics'):
        print("\n  New topics:")
        for t in result['topics']:
            print(f"    • {t}")
    print()


def cmd_fill_calendar(args) -> None:
    """
    Auto-fill the weekly queue from the top scored topics.

    Args:
        args: Parsed argparse namespace with n attribute.
    """
    from database import init_db
    from scheduler import run_auto_fill_calendar
    init_db()

    print(f"\nAuto-filling calendar with top {args.n} scored topics…")
    result = run_auto_fill_calendar(n=args.n)

    if result.get('error'):
        print(f"  ERROR: {result['error']}")
        return

    print(f"\n  Topics queued: {result['queued']}")
    if result.get('topics'):
        for t in result['topics']:
            print(f"    • {t}")
    print()


def cmd_score_topic(args) -> None:
    """
    Score a single topic with the research engine and print the result.

    Args:
        args: Parsed argparse namespace with topic, bucket, id attributes.
    """
    import json
    from database import init_db
    from modules.research_engine import score_topic
    init_db()
    config = load_config()

    print(f"\nScoring: '{args.topic}' (bucket={args.bucket})")
    result = score_topic(
        topic=args.topic,
        bucket=args.bucket,
        config=config,
        topic_id=getattr(args, 'id', 0),
    )
    if not result.get('success'):
        print("  ERROR: Scoring failed")
        return

    print(f"\n  Final score:        {result['final_score']:.1f} / 10")
    print(f"  Trend score:        {result['trend_score']:.1f}")
    print(f"  Competition score:  {result['competition_score']:.1f}  ({result['competition_level']})")
    print(f"  Channel-fit score:  {result['channel_fit_score']:.1f}")
    print(f"  Performance score:  {result['performance_score']:.1f}")
    if result.get('hook_suggestion'):
        print(f"\n  Hook:  {result['hook_suggestion']}")
    if result.get('alt_angles'):
        print("\n  Alt angles:")
        for angle in result['alt_angles']:
            print(f"    • {angle}")
    print()


def cmd_score_unscored(args) -> None:
    """
    Score all unscored topics in the topic bank up to --limit.

    Args:
        args: Parsed argparse namespace with limit attribute.
    """
    from database import init_db, get_topics
    from modules.research_engine import score_topic
    init_db()
    config = load_config()

    unscored = [t for t in get_topics() if not t.get('final_score')][:args.limit]
    if not unscored:
        print("\nNo unscored topics found.\n")
        return

    print(f"\nScoring {len(unscored)} topic(s)…\n")
    for i, t in enumerate(unscored, 1):
        r = score_topic(
            topic=t['topic'],
            bucket=t.get('bucket') or 'elec',
            config=config,
            topic_id=t['id'],
        )
        status = f"{r['final_score']:.1f}/10" if r.get('success') else 'FAILED'
        print(f"  [{i}/{len(unscored)}] {t['topic'][:60]:<60} {status}")

    print("\nDone.\n")


def cmd_create_channel(args) -> None:
    """
    Create a new channel and scaffold its directories.

    Copies the engineering_brief channel as a template (config.json + empty
    prompts/ and assets/ directories). The owner then customises the channel
    config and adds a voice ID before running any jobs.

    Args:
        args: Parsed argparse namespace with slug, name, handle_yt, handle_tt,
              niche, format attributes.
    """
    import shutil
    from database import init_db, create_channel, get_channel

    init_db()
    slug = args.slug.replace(' ', '_').replace('-', '_').lower()

    if get_channel(slug):
        print(f"ERROR: Channel '{slug}' already exists.", file=sys.stderr)
        sys.exit(1)

    # Scaffold directories
    channel_dir = Path(f'channels/{slug}')
    template_dir = Path('channels/engineering_brief')

    (channel_dir / 'prompts').mkdir(parents=True, exist_ok=True)
    (channel_dir / 'assets' / 'music').mkdir(parents=True, exist_ok=True)
    (channel_dir / 'assets' / 'backgrounds').mkdir(parents=True, exist_ok=True)

    # Create a minimal config overlay
    channel_config = {
        "channel": {
            "name": args.name,
            "handle": args.handle_yt.lstrip('@') if args.handle_yt else '',
            "niche": args.niche or '',
            "platforms": ["tiktok", "youtube_shorts"],
            "target_length_seconds": 70
        },
        "pipeline": {
            "visual_mode": "images"
        },
        "voice": {
            "provider": "elevenlabs",
            "voice_id": "SET_VOICE_ID_FOR_THIS_CHANNEL"
        }
    }
    if args.fmt == 'dialogue':
        channel_config['pipeline']['visual_mode'] = 'images'
        channel_config['voice'] = {
            "provider": "elevenlabs",
            "voice_id_alex": "SET_ALEX_VOICE_ID",
            "voice_id_sam": "SET_SAM_VOICE_ID"
        }

    cfg_path = channel_dir / 'config.json'
    with open(cfg_path, 'w', encoding='utf-8') as f:
        json.dump(channel_config, f, indent=2)
        f.write('\n')

    create_channel(
        slug=slug,
        name=args.name,
        handle_yt=args.handle_yt or '',
        handle_tt=args.handle_tt or '',
        niche=args.niche or '',
        fmt=args.fmt or 'single_narrator',
    )

    print(f"\nChannel '{slug}' created.")
    print(f"  Name:    {args.name}")
    print(f"  Dir:     channels/{slug}/")
    print(f"  Config:  channels/{slug}/config.json")
    print()
    print("Next steps:")
    print(f"  1. Edit channels/{slug}/config.json — set voice_id")
    print(f"  2. Add prompts to channels/{slug}/prompts/ (optional — inherits global defaults)")
    print(f"  3. Add music to channels/{slug}/assets/music/ (optional)")
    print(f"  4. Run: python main.py generate-script 'Your topic' --channel {slug}")
    print()


def cmd_list_channels(args) -> None:
    """
    Print all registered channels.

    Args:
        args: Parsed argparse namespace (no specific fields required).
    """
    from database import init_db, get_channels
    init_db()

    channels = get_channels(active_only=False)
    if not channels:
        print("No channels found.")
        return

    print(f"\n{'ID':<25} {'NAME':<30} {'FORMAT':<20} ACTIVE")
    print("-" * 80)
    for ch in channels:
        active = 'yes' if ch.get('active') else 'no'
        print(f"{ch['id']:<25} {ch['name']:<30} {ch.get('format',''):<20} {active}")
    print()


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

    channel = getattr(args, 'channel', None)
    jobs = get_all_jobs(channel_id=channel)
    if not jobs:
        print("No jobs found.")
        return

    print(f"\n{'ID':<6} {'STATUS':<14} {'CHANNEL':<22} {'BUCKET':<8} {'CREATED':<20} TOPIC")
    print("-" * 90)
    for job in jobs:
        topic = job['topic'][:40] + ('…' if len(job['topic']) > 40 else '')
        ch = (job.get('channel_id') or '')[:20]
        print(f"{job['id']:<6} {job['status']:<14} {ch:<22} {job.get('bucket') or '':<8} "
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
    p_script.add_argument('--channel', type=str, default=None, metavar='SLUG',
                          help='Channel slug (default: default_channel from config.json)')

    # generate-voice
    p_voice = subparsers.add_parser('generate-voice', help='Run Stage 2: synthesise speech')
    p_voice.add_argument('job_id', type=str, help='Job ID e.g. 001')

    # generate-images
    p_images = subparsers.add_parser('generate-images', help='Run Stage 3: generate images')
    p_images.add_argument('job_id', type=str, help='Job ID e.g. 001')

    # assemble
    p_assemble = subparsers.add_parser('assemble', help='Run Stage 4: assemble raw MP4')
    p_assemble.add_argument('job_id', type=str, help='Job ID e.g. 001')

    # add-captions
    p_captions = subparsers.add_parser('add-captions', help='Run Stage 5: burn captions into video')
    p_captions.add_argument('job_id', type=str, help='Job ID e.g. 001')

    # generate-metadata
    p_meta = subparsers.add_parser('generate-metadata', help='Run Stage 6: generate SEO metadata')
    p_meta.add_argument('job_id', type=str, help='Job ID e.g. 001')

    # generate-thumbnail
    p_thumb = subparsers.add_parser('generate-thumbnail', help='Run Stage 6b: generate thumbnail')
    p_thumb.add_argument('job_id', type=str, help='Job ID e.g. 001')

    # upload
    p_upload = subparsers.add_parser('upload', help='Run Stage 7: upload to YouTube + TikTok')
    p_upload.add_argument('job_id', type=str, help='Job ID e.g. 001')

    # status
    p_status = subparsers.add_parser('status', help='Show status of a single job')
    p_status.add_argument('job_id', type=str, help='Job ID e.g. 001')

    # list-jobs
    p_lj = subparsers.add_parser('list-jobs', help='List all jobs')
    p_lj.add_argument('--channel', type=str, default=None, metavar='SLUG',
                      help='Filter by channel slug')

    # batch
    p_batch = subparsers.add_parser(
        'batch',
        help='Run pipeline for N queued jobs without waiting for the Sunday schedule'
    )
    p_batch.add_argument(
        '--count', type=int, default=None, metavar='N',
        help='Number of jobs to process (default: batch_size_per_week from config.json)'
    )
    p_batch.add_argument('--channel', type=str, default=None, metavar='SLUG',
                         help='Run batch only for this channel')

    # create-channel (Phase 12)
    p_cc = subparsers.add_parser('create-channel', help='Scaffold a new channel directory and register it')
    p_cc.add_argument('slug', type=str, help='Short identifier e.g. reddit_stories')
    p_cc.add_argument('name', type=str, help='Display name e.g. "Reddit Stories"')
    p_cc.add_argument('--handle-yt', type=str, default='', dest='handle_yt', help='YouTube handle e.g. @MyChannel')
    p_cc.add_argument('--handle-tt', type=str, default='', dest='handle_tt', help='TikTok handle')
    p_cc.add_argument('--niche', type=str, default='', help='Short niche description')
    p_cc.add_argument('--format', type=str, choices=['single_narrator', 'dialogue'],
                      default='single_narrator', dest='fmt', help='Script format (default: single_narrator)')

    # list-channels (Phase 12)
    subparsers.add_parser('list-channels', help='List all registered channels')

    # scan-trends
    subparsers.add_parser('scan-trends', help='Run a Google Trends scan and create priority alerts')

    # scan-reddit
    p_reddit = subparsers.add_parser(
        'scan-reddit',
        help='Scan subreddits for top weekly text posts and add story candidates'
    )
    p_reddit.add_argument('--subs', type=str, default='',
                          help='Comma-separated subreddits e.g. aita,tifu,relationship_advice '
                               '(default: reddit.default_subs from config.json)')
    p_reddit.add_argument('--min-upvotes', type=int, default=None, dest='min_upvotes',
                          metavar='N', help='Minimum post score to accept (default: config)')
    p_reddit.add_argument('--limit', type=int, default=None, metavar='N',
                          help='Max posts to pull per subreddit (default: config)')

    # list-alerts
    subparsers.add_parser('list-alerts', help='Show all active priority alerts')

    # fast-track
    p_ft = subparsers.add_parser('fast-track', help='Fast-track a priority alert through the pipeline')
    p_ft.add_argument('--alert-id', type=int, required=True, metavar='ID',
                      help='Priority alert ID (from list-alerts)')
    p_ft.add_argument('--channel', type=str, default=None, metavar='SLUG',
                      help='Channel to create the job under')

    # add-topic
    p_add_topic = subparsers.add_parser('add-topic', help='Add a topic to the topic bank')
    p_add_topic.add_argument('topic', type=str, help='Topic text')
    p_add_topic.add_argument('--bucket', type=str, choices=['elec', 'infra', 'vehicle', 'flaw'],
                             default='', help='Content bucket')
    p_add_topic.add_argument('--notes', type=str, default='', help='Optional notes')
    p_add_topic.add_argument('--channel', type=str, default=None, metavar='SLUG',
                             help='Channel slug (default: engineering_brief)')

    # archive-topic
    p_arch = subparsers.add_parser('archive-topic', help='Archive a topic in the topic bank')
    p_arch.add_argument('--id', type=int, required=True, metavar='ID', help='Topic bank ID')
    p_arch.add_argument('--reason', type=str, default='', help='Archive reason')

    # export-topics
    p_export = subparsers.add_parser('export-topics', help='Export topic bank to CSV')
    p_export.add_argument('--output', type=str, default='topics_export.csv',
                          help='Output CSV file path')

    # score-topic (11.v2.A)
    p_score = subparsers.add_parser('score-topic', help='Score a topic with the research engine')
    p_score.add_argument('topic', type=str, help='Topic text to score')
    p_score.add_argument('--bucket', type=str, choices=['elec', 'infra', 'vehicle', 'flaw'],
                         default='elec', help='Content bucket')
    p_score.add_argument('--id', type=int, default=0, metavar='TOPIC_ID',
                         help='Topic bank ID to save result back to (optional)')

    # score-unscored (11.v2.A)
    p_su = subparsers.add_parser('score-unscored', help='Score all unscored topics in the bank')
    p_su.add_argument('--limit', type=int, default=20, help='Max topics to score (default 20)')

    # mine-comments (11.v2.C)
    subparsers.add_parser('mine-comments', help='Pull YouTube comments and extract topic ideas')

    # fill-calendar (11.v2.D)
    p_fill = subparsers.add_parser('fill-calendar', help='Auto-queue the top scored topics for this week')
    p_fill.add_argument('--n', type=int, default=5, help='Number of topics to queue (default 5)')

    return parser


COMMAND_MAP = {
    'test-connections':    cmd_test_connections,
    'batch':               cmd_batch,
    'scan-trends':         cmd_scan_trends,
    'scan-reddit':         cmd_scan_reddit,
    'list-alerts':         cmd_list_alerts,
    'fast-track':          cmd_fast_track,
    'add-topic':           cmd_add_topic,
    'archive-topic':       cmd_archive_topic,
    'export-topics':       cmd_export_topics,
    'score-topic':         cmd_score_topic,
    'score-unscored':      cmd_score_unscored,
    'mine-comments':       cmd_mine_comments,
    'fill-calendar':       cmd_fill_calendar,
    'generate-script':     cmd_generate_script,
    'generate-voice':      cmd_generate_voice,
    'generate-images':     cmd_generate_images,
    'assemble':            cmd_assemble,
    'add-captions':        cmd_add_captions,
    'generate-metadata':   cmd_generate_metadata,
    'generate-thumbnail':  cmd_generate_thumbnail,
    'upload':              cmd_upload,
    'status':              cmd_status,
    'list-jobs':           cmd_list_jobs,
    'create-channel':      cmd_create_channel,
    'list-channels':       cmd_list_channels,
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
