"""Broadcast commentary sync: cut tournament-broadcast audio to match highlight clips.

Demo ticks are mapped to VOD seconds through human-verified anchors (anchors.py,
timeline.py); the output is a WAV + JSON sidecar per highlight, never a change to
the highlight clip, report or demo.
"""
