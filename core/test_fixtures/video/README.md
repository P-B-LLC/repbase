# Video test fixtures

Real encoder output, so the upload checks are tested against files a phone or
an editor actually writes rather than against what the parser expects. Each is
64x48 and a few kilobytes. Regenerate with ffmpeg:

```sh
ffmpeg -f lavfi -i testsrc=size=64x48:rate=10 -f lavfi -i sine=frequency=440:sample_rate=8000 -t 1 \
  -c:v libx264 -pix_fmt yuv420p -c:a aac -b:a 16k -movflags +faststart h264_faststart.mp4
ffmpeg -f lavfi -i testsrc=size=64x48:rate=10 -t 1 -c:v libx264 -pix_fmt yuv420p moov_at_end.mp4
ffmpeg -f lavfi -i testsrc=size=64x48:rate=10 -f lavfi -i sine=frequency=440:sample_rate=8000 -t 1 \
  -c:v libx265 -tag:v hvc1 -pix_fmt yuv420p -c:a aac -b:a 16k hevc.mov
ffmpeg -f lavfi -i testsrc=size=64x48:rate=10 -f lavfi -i sine=frequency=440:sample_rate=8000 -t 1 \
  -c:v libx264 -pix_fmt yuv420p -c:a aac -b:a 16k -metadata location="+40.4406-079.9959/" \
  -metadata "com.apple.quicktime.location.ISO6709=+40.4406-079.9959+000.000/" -metadata title="My house" \
  -movflags +use_metadata_tags with_location.mov
ffmpeg -f lavfi -i sine=frequency=440:sample_rate=8000 -t 1 -c:a aac -b:a 16k audio_only.m4a
ffmpeg -f lavfi -i testsrc=size=64x48:rate=10 -t 1 -c:v libx264 -pix_fmt yuv420p \
  -movflags frag_keyframe+empty_moov fragmented.mp4
ffmpeg -f lavfi -i testsrc=size=64x48:rate=10 -t 1 -c:v mpeg4 mpeg4part2.mp4
ffmpeg -f lavfi -i testsrc=size=64x48:rate=10 -t 1 -c:v libvpx-vp9 vp9.webm
ffmpeg -f lavfi -i testsrc=size=64x48:rate=10 -t 3 -c:v libx264 -pix_fmt yuv420p three_seconds.mp4
```
