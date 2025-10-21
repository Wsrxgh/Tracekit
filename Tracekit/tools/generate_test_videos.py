#!/usr/bin/env python3
"""
Generate test videos with different durations and visual patterns for ffmpeg transcoding tests.
Creates 1080p 30fps videos with various noise patterns and visual complexity.
"""

import subprocess
import os
from pathlib import Path

def generate_video(output_path: str, duration: int, pattern_type: str, video_id: int):
    """Generate a single test video using ffmpeg"""
    width, height = 1920, 1080
    fps = 30

    # Per-pattern filtergraphs compatible with FFmpeg 4.2 (avoid geq RGB self-references)
    def fg(pattern: str, noise_level: int) -> str:
        if pattern == "gradient":
            # Bars + slow hue rotation + controlled noise
            base = f"smptehdbars=size={width}x{height}:rate={fps}"
            return f"{base},hue='H=2*PI*t*0.05',noise=alls={noise_level}:allf=t"
        if pattern == "noise":
            # Noise over black; allow 0–5 with emphasis on 0–2
            base = f"color=c=black:size={width}x{height}:rate={fps}"
            return f"{base},format=yuv420p,noise=alls={noise_level}:allf=t"
        if pattern == "moving":
            # Moving test pattern + controlled noise
            base = f"testsrc2=size={width}x{height}:rate={fps}"
            return f"{base},noise=alls={noise_level}:allf=t"
        if pattern == "mandelbrot":
            # Fractal with mild noise and lower iteration for FFmpeg 4.2 performance
            maxiter = 80 + (video_id % 3) * 20  # 80/100/120
            base = f"mandelbrot=size={width}x{height}:rate={fps}:maxiter={maxiter}"
            return f"{base},noise=alls={noise_level}:allf=t"
        # plasma-like: use testsrc2 + hue shift + controlled noise (compatible with 4.2)
        base = f"testsrc2=size={width}x{height}:rate={fps}"
        return f"{base},hue='H=2*PI*t*0.1',noise=alls={noise_level}:allf=t"

    # Pattern-specific noise distributions (main vs minor ranges)
    import random
    rng = random.Random((duration * 10) + video_id)
    if pattern_type == "gradient":
        # main 8–12 (80%), minor 13–15 (20%)
        noise_level = rng.randint(8, 12) if rng.random() < 0.8 else rng.randint(13, 15)
    elif pattern_type == "mandelbrot":
        # main 6–8, minor 9–10
        noise_level = rng.randint(6, 8) if rng.random() < 0.8 else rng.randint(9, 10)
    elif pattern_type == "moving":
        # main 4–6, minor 7–8
        noise_level = rng.randint(4, 6) if rng.random() < 0.8 else rng.randint(7, 8)
    elif pattern_type == "plasma":
        # main 5–9, minor 10–12
        noise_level = rng.randint(5, 9) if rng.random() < 0.8 else rng.randint(10, 12)
    else:  # noise
        # main 0–2, minor 3–5
        noise_level = rng.randint(0, 2) if rng.random() < 0.8 else rng.randint(3, 5)

    filtergraph = fg(pattern_type, noise_level)

    # Encode to H.264 yuv420p, target ~8 Mbps ABR, GOP ~90 (3s @30fps) for consistent decode cost
    cmd = [
        "ffmpeg", "-y", "-loglevel", "info", "-stats",
        "-f", "lavfi", "-i", filtergraph,
        "-t", str(duration),
        "-c:v", "libx264", "-preset", "medium",
        "-b:v", "8M", "-maxrate", "8M", "-bufsize", "16M",
        "-g", "90", "-x264-params", "keyint=90:min-keyint=60:scenecut=40",
        "-pix_fmt", "yuv420p", "-r", str(fps),
        str(output_path),
    ]

    print(f"Generating {output_path} ({duration}s, {pattern_type})...")
    try:
        # Show progress; do not capture output so you can see ffmpeg ETA
        subprocess.run(cmd, check=True)
        print(f"✓ Generated {output_path}")
    except subprocess.CalledProcessError as e:
        print(f"✗ Failed to generate {output_path}: {e}")

def main():
    # Create inputs directory
    inputs_dir = Path("inputs/ffmpeg")
    inputs_dir.mkdir(parents=True, exist_ok=True)
    
    # Duration configurations
    durations = [30, 60, 120, 180]  # seconds
    videos_per_duration = 5
    
    # Pattern types for variety
    patterns = ["gradient", "noise", "moving", "mandelbrot", "plasma"]
    
    total_videos = len(durations) * videos_per_duration
    current = 0
    
    for duration in durations:
        for i in range(videos_per_duration):
            current += 1
            pattern = patterns[i % len(patterns)]
            filename = f"test_{duration}s_{pattern}_{i+1:02d}.mp4"
            output_path = inputs_dir / filename
            
            print(f"[{current}/{total_videos}] Creating {filename}")
            generate_video(output_path, duration, pattern, i+1)
    
    print(f"\n✓ Generated {total_videos} test videos in {inputs_dir}")
    print("\nVideo summary:")
    for duration in durations:
        count = len(list(inputs_dir.glob(f"test_{duration}s_*.mp4")))
        print(f"  {duration}s videos: {count}")
    
    total_size = sum(f.stat().st_size for f in inputs_dir.glob("*.mp4")) / (1024*1024)
    print(f"  Total size: {total_size:.1f} MB")

if __name__ == "__main__":
    main()
