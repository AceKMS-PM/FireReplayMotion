#!/bin/bash
# Phase 0 demo: each block is ONE ffmpeg command = one effect.
# Run this file, then watch the outputs in your video player.
set -e

# ---------------------------------------------------------------
# 1. CUT: keep only 9s -> 13s (this jumps over the 6-9 silence)
#    -ss start  -to end   (-ss BEFORE -i = fast seek)
#    Re-encoding (libx264) = frame-accurate cut.
# ---------------------------------------------------------------
ffmpeg -y -ss 9 -to 13 -i test_clip.mp4 -c:v libx264 -preset veryfast -crf 23 -c:a aac demo_1_cut.mp4

# ---------------------------------------------------------------
# 2. ZOOM: crop the middle quarter of the image, scale it back to 720p
#    crop=w:h:x:y  ->  here: half width, half height, centered
# ---------------------------------------------------------------
ffmpeg -y -i test_clip.mp4 -vf "crop=iw/2:ih/2:iw/4:ih/4,scale=1280:720" -c:v libx264 -preset veryfast -crf 23 -c:a copy demo_2_zoom.mp4

# ---------------------------------------------------------------
# 3. TEXT + FADE: write a title for the first 3 seconds,
#    fade from black at the start
#    drawtext 'enable' limits when the text is visible
# ---------------------------------------------------------------
ffmpeg -y -i test_clip.mp4 -vf "drawtext=text='Chapter 1':x=(w-tw)/2:y=60:fontsize=60:fontcolor=white:borderw=2:enable='lt(t,3)',fade=t=in:st=0:d=1" -c:v libx264 -preset veryfast -crf 23 -c:a copy demo_3_text_fade.mp4

# ---------------------------------------------------------------
# 4. POLISH: color adjustments (saturation, brightness, contrast)
#    eq=saturation:brightness:contrast  -- subtle but visible
# ---------------------------------------------------------------
ffmpeg -y -i test_clip.mp4 -vf "eq=saturation=1.4:brightness=0.05:contrast=1.1" -c:v libx264 -preset veryfast -crf 23 -c:a copy demo_4_polish.mp4

echo "Done. Outputs: demo_1_cut / demo_2_zoom / demo_3_text_fade / demo_4_polish"
